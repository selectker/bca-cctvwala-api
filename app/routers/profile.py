"""My profile: view / edit my own details, change my own password, see my own activity.

Works for every signed-in account, including the platform admin (it only ever sees its own record).
Staff / technician accounts have a PIN, not a password: they change it with POST /auth/change-pin.
"""
import asyncio

import asyncpg
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status

from app.config import settings

from app.db import get_pool
from app.limiter import credential_limit
from app.schemas.profile import ChangePasswordRequest, UpdateProfileRequest
from app.security import get_current_user, hash_password, issue_session_token, password_policy_error, revoke_user_sessions, verify_password
from app.utils import avatar as avatar_util
from app.utils import storage
from app.utils.audit import AuditFilters, client_ip, fetch_logs, log_event
from app.utils.login_lock import clear_failures, ensure_not_locked, record_failure
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict

router = APIRouter(prefix="/profile", tags=["profile"])

_COLS = "id, name, phone, email, role, is_platform_admin, must_change_pin, created_at, avatar_key, avatar_updated_at"
_STAFF_ROLES = ("STAFF", "TECHNICIAN")


def _shape(row, last_login_at) -> dict:
    data = row_to_dict(row)
    # Tells the app which credential screen to offer: PIN (POST /auth/change-pin) or password (POST /profile/change-password).
    data["credential"] = "PIN" if data["role"] in _STAFF_ROLES else "PASSWORD"
    data["last_login_at"] = last_login_at
    # The storage key is never exposed: the app sees `has_image` and fetches the picture with GET /profile/image
    # (`image_updated_at` works as a cache-buster).
    data["has_image"] = bool(data.pop("avatar_key", None))
    data["image_updated_at"] = data.pop("avatar_updated_at", None)
    return data


async def _last_login(conn, user_id: str):
    return await conn.fetchval(
        "SELECT created_at FROM audit_logs WHERE user_id = $1::uuid AND action = 'LOGIN' ORDER BY created_at DESC, id DESC LIMIT 1",
        user_id,
    )


@router.get("")
async def get_profile(current_user=Depends(get_current_user)):
    """The signed-in user's own details. `last_login_at` is the latest sign-in on record (null if none yet)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {_COLS} FROM users WHERE id = $1::uuid", current_user["id"])
        last_login = await _last_login(conn, current_user["id"])
    return _shape(row, last_login)


@router.put("")
async def update_profile(body: UpdateProfileRequest, request: Request, current_user=Depends(get_current_user)):
    """Change my own name, phone or email -- only the fields sent. Role, active state and credentials can't
    be changed here. The owner / platform admin sign in with their phone number, so after changing it they
    sign in with the new one. 409 when the phone or email already belongs to another account."""
    sent = body.model_fields_set
    if not sent & {"name", "phone", "email"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nothing to update")
    for field in ("name", "phone"):
        if field in sent and not getattr(body, field):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field} can't be empty")

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(f"SELECT {_COLS} FROM users WHERE id = $1::uuid FOR UPDATE", current_user["id"])
                if existing is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
                new = {f: getattr(body, f) if f in sent else existing[f] for f in ("name", "phone", "email")}
                changed = {f: {"from": existing[f], "to": new[f]} for f in new if new[f] != existing[f]}
                if changed:
                    row = await conn.fetchrow(
                        f"UPDATE users SET name = $1, phone = $2, email = $3 WHERE id = $4::uuid RETURNING {_COLS}",
                        new["name"], new["phone"], new["email"], current_user["id"],
                    )
                else:
                    row = existing
            last_login = await _last_login(conn, current_user["id"])
    except asyncpg.UniqueViolationError as exc:
        which = "Email" if "email" in (exc.constraint_name or "") else "Phone"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{which} is already used by another account")

    if changed:
        await log_event(
            {**current_user, "name": row["name"]}, "UPDATE_PROFILE", "USER", current_user["id"],
            summary=f"Updated own profile ({', '.join(changed)})", details={"changed": changed}, request=request,
        )
    return _shape(row, last_login)


@router.post("/change-password")
@credential_limit
async def change_password(body: ChangePasswordRequest, request: Request, current_user=Depends(get_current_user)):
    """Owner / platform admin only (staff and technicians use POST /auth/change-pin). Needs the current
    password. Every other sign-in of this account stops working; the response carries a fresh token for
    this device, same as change-pin."""
    if current_user["role"] in _STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff and technician accounts use a PIN — change it with /auth/change-pin")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The new password must be different from the current one")
    weak = password_policy_error(body.new_password)
    if weak:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=weak)

    pool = get_pool()
    ip = client_ip(request)
    subject = str(current_user["id"])
    async with pool.acquire() as conn:
        # Wrong current passwords count toward the same lock as sign-in (v1.17.3): a stolen session token can't be
        # used to guess the password through this endpoint. Checked before the password is verified.
        await ensure_not_locked(conn, subject, current_user, ip)
        existing = await conn.fetchrow("SELECT password_hash FROM users WHERE id = $1::uuid", current_user["id"])
    # (verified with no connection held, so a wrong guess can record itself on its own connection)
    if existing is None or not await asyncio.to_thread(verify_password, body.current_password, existing["password_hash"]):
        await record_failure(subject, current_user, ip, request, during="change-password")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    new_hash = await asyncio.to_thread(hash_password, body.new_password)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"UPDATE users SET password_hash = $1, password_changed_at = now() WHERE id = $2::uuid RETURNING {_COLS}",
                new_hash, current_user["id"],
            )
            await revoke_user_sessions(conn, current_user["id"])  # every other device is signed out
            token = await issue_session_token(conn, str(row["id"]), row["name"], row["role"], request)
        await clear_failures(conn, subject, ip)
        last_login = await _last_login(conn, current_user["id"])

    await log_event(current_user, "CHANGE_PASSWORD", "USER", current_user["id"], request=request)
    return {"token": token, "user": _shape(row, last_login)}


@router.get("/image")
async def get_profile_image(current_user=Depends(get_current_user)):
    """My profile image as a JPEG. 404 when none has been uploaded."""
    pool = get_pool()
    async with pool.acquire() as conn:
        key = await conn.fetchval("SELECT avatar_key FROM users WHERE id = $1::uuid", current_user["id"])
    return await avatar_util.image_response(key)


@router.post("/image")
async def upload_profile_image(request: Request, image: UploadFile = File(...), current_user=Depends(get_current_user)):
    """Set my profile image. Multipart form, field `image`: a JPEG, PNG or WebP up to MAX_PROFILE_IMAGE_MB (default 2).
    It is cropped to a centred square, shrunk to at most 512 x 512 and stored as a JPEG with all metadata removed,
    replacing any earlier image. Returns the profile (`has_image`, `image_updated_at`). 400 for an unusable file."""
    max_bytes = settings.max_profile_image_mb * 1024 * 1024
    data = await image.read(max_bytes + 1)
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose an image file")
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"The image must be under {settings.max_profile_image_mb} MB")
    try:
        jpeg = await avatar_util.process(data)
    except avatar_util.AvatarError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    key = avatar_util.new_key()
    try:
        await asyncio.to_thread(storage.put_bytes, key, jpeg, avatar_util.CONTENT_TYPE)
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Image storage is not reachable, try again")

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                old_key = await conn.fetchval("SELECT avatar_key FROM users WHERE id = $1::uuid FOR UPDATE", current_user["id"])
                row = await conn.fetchrow(
                    f"UPDATE users SET avatar_key = $1, avatar_updated_at = now() WHERE id = $2::uuid RETURNING {_COLS}",
                    key, current_user["id"],
                )
                if row is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            last_login = await _last_login(conn, current_user["id"])
    except Exception:
        await asyncio.to_thread(storage.delete_keys, [key])  # don't leave an orphan image behind
        raise
    if old_key:
        await asyncio.to_thread(storage.delete_keys, [old_key])
    await log_event(current_user, "UPDATE_PROFILE_IMAGE", "USER", current_user["id"],
                    summary="Changed own profile image", request=request)
    return _shape(row, last_login)


@router.delete("/image")
async def remove_profile_image(request: Request, current_user=Depends(get_current_user)):
    """Remove my profile image (nothing happens if there is none). Returns the profile."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_key = await conn.fetchval("SELECT avatar_key FROM users WHERE id = $1::uuid FOR UPDATE", current_user["id"])
            row = await conn.fetchrow(
                f"UPDATE users SET avatar_key = NULL, avatar_updated_at = CASE WHEN avatar_key IS NULL THEN avatar_updated_at ELSE now() END "
                f"WHERE id = $1::uuid RETURNING {_COLS}",
                current_user["id"],
            )
        last_login = await _last_login(conn, current_user["id"])
    if old_key:
        await asyncio.to_thread(storage.delete_keys, [old_key])
        await log_event(current_user, "UPDATE_PROFILE_IMAGE", "USER", current_user["id"],
                        summary="Removed own profile image", request=request)
    return _shape(row, last_login)


@router.get("/activity")
async def my_activity(filters: AuditFilters = Depends(), page: Page = Depends(), current_user=Depends(get_current_user)):
    """What I have done, newest first (paged, X-Total-Count): every write I made and every sign-in. Takes the
    same filters as GET /audit-logs except user_id, which is always me."""
    filters.user_id = current_user["id"]
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await fetch_logs(conn, scope="all", filters=filters, limit=page.limit, offset=page.offset)
    return paged_response(rows, extra_drop=("user_id", "user_name", "user_role", "ip_address"))
