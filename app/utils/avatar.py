"""Profile images (v1.25.0): validate / normalise an upload before it goes to R2.

Every upload is decoded with Pillow, cropped to a centred square, scaled down to SIZE_PX and re-encoded as a fresh JPEG,
so what is stored is always small and clean: no EXIF (GPS position, camera serial), no XMP / ICC blobs, no odd formats,
no decompression bombs. The rotation flag is applied first so a phone photo still comes out the right way up.
Decoding goes through image_gate, the same cap on concurrent image jobs the photos, logo and signature use.
"""
from __future__ import annotations

import asyncio
import uuid
from io import BytesIO

from fastapi import HTTPException, Response, status

from app.utils import image_gate, storage

SIZE_PX = 512                      # shown as a circle / small square in the app; plenty even on a large phone screen
JPEG_QUALITY = 85
CONTENT_TYPE = "image/jpeg"
PREFIX = "users/profile/"
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_SOURCE_PIXELS = {"JPEG": 100_000_000, "PNG": 25_000_000, "WEBP": 25_000_000}  # refused before decoding


class AvatarError(ValueError):
    """The uploaded file can't be used as a profile image; the message is safe to show the user."""


def normalise_avatar(data: bytes) -> bytes:
    """Upload bytes -> square JPEG bytes (SIZE_PX x SIZE_PX, or smaller for a small source). Blocking (CPU)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            if img.format not in ALLOWED_FORMATS:
                raise AvatarError("Only JPEG, PNG or WebP images can be used as a profile image")
            if img.width * img.height > MAX_SOURCE_PIXELS[img.format]:
                raise AvatarError("That image has too many pixels; use the phone's normal camera setting")
            if img.format == "JPEG":
                img.draft("RGB", (SIZE_PX * 2, SIZE_PX * 2))  # the decoder shrinks it while reading
            img.load()
            img = ImageOps.exif_transpose(img)  # honour the rotation flag before the metadata is dropped
            if "A" in img.getbands() or "transparency" in img.info:
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
    except AvatarError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise AvatarError("That file is not a valid JPEG, PNG or WebP image")

    side = min(min(img.size), SIZE_PX)  # the largest centred square that fits; a small image is never scaled up
    img = ImageOps.fit(img, (side, side), Image.LANCZOS, centering=(0.5, 0.5))
    out = BytesIO()
    # Nothing is passed for exif= / icc_profile= / xmp=, so none of it is written.
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


async def process(data: bytes) -> bytes:
    """normalise_avatar off the event loop, with a cap on how many image jobs run at once (see image_gate)."""
    return await image_gate.run_limited(normalise_avatar, data)


def new_key() -> str:
    return f"{PREFIX}{uuid.uuid4().hex}.jpg"


async def image_response(key: str | None) -> Response:
    """The stored picture as an HTTP response, or the 404 / 502 to send. Shared by every endpoint that serves one."""
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No profile image has been uploaded")
    try:
        data = await asyncio.to_thread(storage.get_bytes, key)
    except storage.ObjectMissing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The profile image file is missing on the server")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Image storage is not reachable, try again")
    # Replacing an image changes the key, but the URL stays the same, so the app must revalidate rather than cache for good.
    return Response(content=data, media_type=CONTENT_TYPE,
                    headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"})
