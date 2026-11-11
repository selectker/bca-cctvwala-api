"""Shop logo: validate / normalise an upload, keep it in R2, and load it for the PDF header.

An uploaded logo is decoded with Pillow, scaled down to fit the invoice header and stored as a PNG, so what is kept is
always a small, clean image (no metadata, no odd formats, no decompression bombs) that reportlab can always draw.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from io import BytesIO

from app.utils import storage

log = logging.getLogger("logo")

MAX_SIDE_PX = 600          # plenty for a ~110 x 56 pt header slot, even printed at 300 dpi
MAX_SOURCE_PIXELS = 12_000_000  # refuse large images before decoding them (a logo never needs more than ~3400 x 3400)
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}
LOGO_PREFIX = "store/logo/"


class LogoError(ValueError):
    """The uploaded file can't be used as a logo; the message is safe to show the user."""


def normalise_logo(data: bytes) -> bytes:
    """Upload bytes -> PNG bytes no larger than MAX_SIDE_PX on either side. Blocking (CPU)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            if img.format not in ALLOWED_FORMATS:
                raise LogoError("Only PNG, JPEG or WebP images can be used as a logo")
            if img.width * img.height > MAX_SOURCE_PIXELS:
                raise LogoError("That image is too large; use one under about 3400 x 3400 pixels")
            if img.format == "JPEG":
                img.draft("RGB", (MAX_SIDE_PX * 2, MAX_SIDE_PX * 2))  # the decoder shrinks it while reading
            img.load()
            img = ImageOps.exif_transpose(img)  # phone photos: honour the rotation flag
            img = img.convert("RGBA")
    except LogoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise LogoError("That file is not a valid PNG, JPEG or WebP image")

    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def new_key() -> str:
    return f"{LOGO_PREFIX}{uuid.uuid4().hex}.png"


async def load_logo_bytes(store: dict | None) -> bytes | None:
    """The logo image for a PDF, or None. Best effort on purpose: a storage hiccup must never stop an invoice printing."""
    key = (store or {}).get("logo_key")
    if not key:
        return None
    try:
        return await asyncio.to_thread(storage.get_bytes, key)
    except Exception:
        log.warning("could not load the store logo (%s); printing without it", key)
        return None
