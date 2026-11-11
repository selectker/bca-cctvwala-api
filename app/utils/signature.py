"""Authorised signature (v1.16.0): decode a base64 image, normalise it, keep it in R2, load it for the invoice PDF.

The owner sends the signature as base64 text (raw, or a `data:image/png;base64,...` data URL, which is what a signature
pad or a canvas gives you). It is decoded with Pillow, trimmed to the ink, scaled down and stored as a PNG, so what is
kept is always a small, clean image that reportlab can always draw.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import uuid
from io import BytesIO

from app.utils import storage

log = logging.getLogger("signature")

MAX_SIDE_PX = 600
MAX_SOURCE_PIXELS = 12_000_000  # a signature never needs more than ~3400 x 3400
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}
SIGNATURE_PREFIX = "store/signature/"
_DATA_URL = re.compile(r"^data:[^;,]*;base64,", re.IGNORECASE)


class SignatureError(ValueError):
    """The signature can't be used; the message is safe to show the user."""


def decode_base64(text: str) -> bytes:
    """Base64 text (raw or a data URL, line breaks and spaces allowed, URL-safe alphabet allowed) -> bytes."""
    if not isinstance(text, str) or not text.strip():
        raise SignatureError("Send the signature image as base64 text")
    text = _DATA_URL.sub("", text.strip(), count=1)
    text = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("That is not valid base64")
    if not data:
        raise SignatureError("Send the signature image as base64 text")
    return data


def normalise_signature(data: bytes) -> bytes:
    """Image bytes -> PNG bytes, empty margins trimmed, at most MAX_SIDE_PX on either side. Blocking (CPU)."""
    from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            if img.format not in ALLOWED_FORMATS:
                raise SignatureError("Only PNG, JPEG or WebP images can be used as a signature")
            if img.width * img.height > MAX_SOURCE_PIXELS:
                raise SignatureError("That image is too large; use one under about 3400 x 3400 pixels")
            if img.format == "JPEG":
                img.draft("RGB", (MAX_SIDE_PX * 3, MAX_SIDE_PX * 3))  # the decoder shrinks it while reading
            img.load()
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGBA")
    except SignatureError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise SignatureError("That is not a valid PNG, JPEG or WebP image")

    # Trim the empty border so the signature sits properly on the line: transparent pixels, or else a plain white page.
    alpha = img.getchannel("A")
    box = alpha.getbbox()
    if box and alpha.getextrema()[0] == 0:
        img = img.crop(box)
    else:
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        box = ImageChops.difference(img, white).convert("L").point(lambda v: 255 if v > 24 else 0).getbbox()
        if box:
            img = img.crop(box)
    if img.width < 2 or img.height < 2:
        raise SignatureError("That image looks empty")

    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def new_key() -> str:
    return f"{SIGNATURE_PREFIX}{uuid.uuid4().hex}.png"


async def load_signature_bytes(store: dict | None) -> bytes | None:
    """The signature image for a PDF, or None. Best effort on purpose: a storage hiccup must never stop an invoice printing."""
    key = (store or {}).get("signature_key")
    if not key:
        return None
    try:
        return await asyncio.to_thread(storage.get_bytes, key)
    except Exception:
        log.warning("could not load the signature (%s); printing without it", key)
        return None
