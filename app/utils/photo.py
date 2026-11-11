"""Ticket photos: validate / normalise an upload before it goes to R2.

Every photo is decoded with Pillow and re-encoded as a fresh JPEG, so what is stored never carries the original file's
metadata: no EXIF (GPS position of the customer's site, camera serial, timestamps), no XMP, no ICC blobs, no trailing
bytes. The rotation flag is applied first so the picture still looks right. Photos are also scaled down to
MAX_SIDE_PX, which keeps R2 storage and every later download small.

Memory: callers process ONE upload at a time and keep only the small normalised result. JPEGs are decoded with
Image.draft(), i.e. the decoder itself shrinks the picture while reading it, so a 50 MP phone photo never exists in
memory at full size. PNG / WebP can't do that, so they get a lower pixel ceiling. At most image_gate.MAX_CONCURRENT_JOBS images (photos, the
logo, the signature) are processed at the same time across the whole worker, however many uploads are in flight.
"""
from __future__ import annotations

from io import BytesIO

from app.utils import image_gate

MAX_SIDE_PX = 2048                    # plenty to show finished work; phone photos are ~4000 px
JPEG_QUALITY = 85
CONTENT_TYPE = "image/jpeg"
EXTENSION = "jpg"
MAX_SOURCE_PIXELS = {"JPEG": 100_000_000, "PNG": 25_000_000, "WEBP": 25_000_000}  # refused before decoding


class PhotoError(ValueError):
    """The uploaded file can't be used as a photo; the message is safe to show the user."""


def normalise_photo(data: bytes) -> bytes:
    """Upload bytes -> clean JPEG bytes, at most MAX_SIDE_PX on either side. Blocking (CPU)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            limit = MAX_SOURCE_PIXELS.get(img.format or "")
            if limit is None:
                raise PhotoError("Only JPEG, PNG or WebP photos can be uploaded")
            if img.width * img.height > limit:
                raise PhotoError("That photo has too many pixels; use the phone's normal camera setting")
            if img.format == "JPEG":
                img.draft("RGB", (MAX_SIDE_PX, MAX_SIDE_PX))  # decode at 1/2, 1/4 or 1/8 size when it's much bigger
            img.load()
            img = ImageOps.exif_transpose(img)  # honour the rotation flag before the metadata is dropped
            if "A" in img.getbands() or "transparency" in img.info:
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise PhotoError("That file is not a valid JPEG, PNG or WebP photo")

    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.LANCZOS)
    out = BytesIO()
    # Nothing is passed for exif= / icc_profile= / xmp=, so none of it is written.
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


async def process(data: bytes) -> bytes:
    """normalise_photo off the event loop, with a cap on how many image jobs run at once (see image_gate)."""
    return await image_gate.run_limited(normalise_photo, data)
