"""The scanned / photographed supplier invoice kept with a purchase.

An image (PNG, JPEG, WebP) is decoded with Pillow, rotated by its EXIF flag, shrunk to MAX_SIDE_PX and stored as a JPEG, so
what is kept is always a clean, reasonably small picture. A PDF is checked for the %PDF header and kept as it is.
"""
from __future__ import annotations

import uuid
from io import BytesIO

PREFIX = "purchases/attachments/"
MAX_SIDE_PX = 2600
MAX_SOURCE_PIXELS = 40_000_000
ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}


class AttachmentError(ValueError):
    """The file can't be kept; the message is safe to show the user."""


def normalise(data: bytes) -> tuple[bytes, str, str]:
    """Upload bytes -> (stored bytes, content type, extension). Blocking (CPU)."""
    if data[:5] == b"%PDF-":
        return data, "application/pdf", "pdf"
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            if img.format not in ALLOWED_IMAGE_FORMATS:
                raise AttachmentError("Only a PDF or a PNG, JPEG or WebP image can be attached")
            if img.width * img.height > MAX_SOURCE_PIXELS:
                raise AttachmentError("That image is too large")
            if img.format == "JPEG":
                img.draft("RGB", (MAX_SIDE_PX * 2, MAX_SIDE_PX * 2))
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[-1])
                img = background
            else:
                img = img.convert("RGB")
    except AttachmentError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise AttachmentError("That file is not a valid PDF or image")
    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue(), "image/jpeg", "jpg"


def new_key(extension: str) -> str:
    return f"{PREFIX}{uuid.uuid4().hex}.{extension}"
