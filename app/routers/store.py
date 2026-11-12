import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status

from app.config import settings
from app.db import get_pool
from app.schemas.store import SignatureBase64Request, UpdateStoreProfileRequest
from app.security import get_current_user, require_role
from app.utils import logo as logo_util
from app.utils import image_gate
from app.utils import signature as signature_util
from app.utils import storage
from app.utils.rows import row_to_dict
from app.utils.audit import AuditRoute

router = APIRouter(route_class=AuditRoute, prefix="/store-profile", tags=["store"])

# Staff / technicians only need the shop's name, address and contact; the bank account is for the owner.
_OWNER_ONLY_FIELDS = ("bank_name", "bank_account_no", "bank_ifsc", "upi_id")


def _present(row) -> dict:
    """The profile as the app sees it: the internal storage key is replaced by `has_logo`
    (the image itself is fetched with GET /store-profile/logo; `logo_updated_at` works as a cache-buster);
    the same goes for the signature (`has_signature`, GET /store-profile/signature, `signature_updated_at`)."""
    data = row_to_dict(row) or {}
    data["has_logo"] = bool(data.pop("logo_key", None))
    data["has_signature"] = bool(data.pop("signature_key", None))
    return data


@router.get("")
async def get_store_profile(user=Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    data = _present(row)
    if user["role"] != "OWNER":
        data = {k: v for k, v in data.items() if k not in _OWNER_ONLY_FIELDS}
    return data


@router.put("")
async def update_store_profile(body: UpdateStoreProfileRequest, _owner=Depends(require_role("OWNER"))):
    """Invoice header, GSTIN, bank details and document prefixes — no SQL needed any more."""
    def blank_to_none(v):
        return v.strip() or None if isinstance(v, str) else v

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO store_profile (id, store_name, gstin, address, state, state_code, phone, email,
                                          bank_name, bank_account_no, bank_ifsc, invoice_prefix, quotation_prefix, upi_id)
               VALUES (1,$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$14)
               ON CONFLICT (id) DO UPDATE SET
                 store_name=EXCLUDED.store_name, gstin=EXCLUDED.gstin, address=EXCLUDED.address,
                 state=EXCLUDED.state, state_code=EXCLUDED.state_code, phone=EXCLUDED.phone, email=EXCLUDED.email,
                 bank_name=EXCLUDED.bank_name, bank_account_no=EXCLUDED.bank_account_no, bank_ifsc=EXCLUDED.bank_ifsc,
                 invoice_prefix=EXCLUDED.invoice_prefix, quotation_prefix=EXCLUDED.quotation_prefix,
                 upi_id = CASE WHEN $13 THEN EXCLUDED.upi_id ELSE store_profile.upi_id END
               RETURNING *""",
            body.store_name.strip(), blank_to_none(body.gstin), blank_to_none(body.address), blank_to_none(body.state),
            body.state_code, blank_to_none(body.phone), blank_to_none(body.email), blank_to_none(body.bank_name),
            blank_to_none(body.bank_account_no), blank_to_none(body.bank_ifsc), body.invoice_prefix, body.quotation_prefix,
            "upi_id" in body.model_fields_set, body.upi_id,
        )
    return _present(row)


# ---------- logo (printed in the header of invoice and quotation PDFs) ----------
@router.get("/logo")
async def get_logo(user=Depends(get_current_user)):
    """The shop logo as a PNG (any signed-in user). 404 when none has been uploaded."""
    pool = get_pool()
    async with pool.acquire() as conn:
        key = await conn.fetchval("SELECT logo_key FROM store_profile WHERE id = 1")
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo has been uploaded")
    try:
        data = await asyncio.to_thread(storage.get_bytes, key)
    except storage.ObjectMissing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The logo file is missing on the server")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Logo storage is not reachable, try again")
    # Replacing the logo changes the key, but the URL stays the same, so the app must revalidate rather than cache for good.
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"})


@router.post("/logo")
async def upload_logo(logo: UploadFile = File(...), _owner=Depends(require_role("OWNER"))):
    """Owner only. Multipart form, field `logo`: a PNG, JPEG or WebP (up to MAX_LOGO_MB). It is shrunk and stored as a
    PNG, replaces any earlier logo, and is printed top-left on every invoice and quotation PDF from now on
    (PDFs already downloaded are not changed). A transparent PNG looks best."""
    max_bytes = settings.max_logo_mb * 1024 * 1024
    data = await logo.read(max_bytes + 1)
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose an image file")
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"The logo must be under {settings.max_logo_mb} MB")
    try:
        png = await image_gate.run_limited(logo_util.normalise_logo, data)
    except logo_util.LogoError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    key = logo_util.new_key()
    try:
        await asyncio.to_thread(storage.put_bytes, key, png, "image/png")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Logo storage is not reachable, try again")

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                old_key = await conn.fetchval("SELECT logo_key FROM store_profile WHERE id = 1 FOR UPDATE")
                row = await conn.fetchrow(
                    "UPDATE store_profile SET logo_key = $1, logo_updated_at = now() WHERE id = 1 RETURNING *", key
                )
                if row is None:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Save the store profile first")
    except Exception:
        await asyncio.to_thread(storage.delete_keys, [key])  # don't leave an orphan image behind
        raise
    if old_key:
        await asyncio.to_thread(storage.delete_keys, [old_key])
    return _present(row)


@router.delete("/logo")
async def remove_logo(_owner=Depends(require_role("OWNER"))):
    """Owner only. Invoices and quotations print without a logo again."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_key = await conn.fetchval("SELECT logo_key FROM store_profile WHERE id = 1 FOR UPDATE")
            row = await conn.fetchrow(
                "UPDATE store_profile SET logo_key = NULL, logo_updated_at = now() WHERE id = 1 RETURNING *"
            )
    if old_key:
        await asyncio.to_thread(storage.delete_keys, [old_key])
    return _present(row)


# ---------- signature (printed above "Authorised Signatory" on invoice PDFs; sent as base64) ----------
@router.get("/signature")
async def get_signature(_owner=Depends(require_role("OWNER"))):
    """The signature as a PNG. OWNER only (v1.17.2): the image is a forgery tool, so staff and technicians cannot
    download it (the invoice PDFs they print still carry it, and `has_signature` in GET /store-profile still tells
    them whether one is set). 404 when none has been saved."""
    pool = get_pool()
    async with pool.acquire() as conn:
        key = await conn.fetchval("SELECT signature_key FROM store_profile WHERE id = 1")
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No signature has been saved")
    try:
        data = await asyncio.to_thread(storage.get_bytes, key)
    except storage.ObjectMissing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The signature file is missing on the server")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Signature storage is not reachable, try again")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"})


@router.put("/signature")
async def save_signature(body: SignatureBase64Request, _owner=Depends(require_role("OWNER"))):
    """Owner only. JSON `{"signature_base64": "..."}`: a PNG, JPEG or WebP as base64 text (raw, or a `data:image/png;base64,`
    data URL). It is decoded, trimmed, shrunk and stored as a PNG, replaces any earlier signature, and is printed on
    every invoice PDF from now on (PDFs already downloaded are not changed)."""
    try:
        raw = await asyncio.to_thread(signature_util.decode_base64, body.signature_base64)
        if len(raw) > settings.max_signature_mb * 1024 * 1024:
            raise signature_util.SignatureError(f"The signature image must be under {settings.max_signature_mb} MB")
        png = await image_gate.run_limited(signature_util.normalise_signature, raw)
    except signature_util.SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    key = signature_util.new_key()
    try:
        await asyncio.to_thread(storage.put_bytes, key, png, "image/png")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Signature storage is not reachable, try again")

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                old_key = await conn.fetchval("SELECT signature_key FROM store_profile WHERE id = 1 FOR UPDATE")
                row = await conn.fetchrow(
                    "UPDATE store_profile SET signature_key = $1, signature_updated_at = now() WHERE id = 1 RETURNING *", key
                )
                if row is None:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Save the store profile first")
    except Exception:
        await asyncio.to_thread(storage.delete_keys, [key])  # don't leave an orphan image behind
        raise
    if old_key:
        await asyncio.to_thread(storage.delete_keys, [old_key])
    return _present(row)


@router.delete("/signature")
async def remove_signature(_owner=Depends(require_role("OWNER"))):
    """Owner only. Invoices print without a signature again."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_key = await conn.fetchval("SELECT signature_key FROM store_profile WHERE id = 1 FOR UPDATE")
            row = await conn.fetchrow(
                "UPDATE store_profile SET signature_key = NULL, signature_updated_at = now() WHERE id = 1 RETURNING *"
            )
    if old_key:
        await asyncio.to_thread(storage.delete_keys, [old_key])
    return _present(row)
