import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config import settings
from app.db import get_pool
from app.limiter import credential_limit, public_limit
from app.schemas.auth import (
    BootstrapRequest,
    ChangePinRequest,
    CreateStaffRequest,
    LoginPinRequest,
    LoginRequest,
    UpdateUserRequest,
)
from app.security import (
    burn_password_check,
    generate_pin,
    get_current_user,
    hash_password,
    issue_session_token,
    require_role,
    require_setup_token,
    revoke_user_sessions,
    verify_password,
)
from app.utils import avatar as avatar_util
from app.utils.audit import AuditRoute, client_ip, log_event
from app.utils.login_lock import (
    actor as _actor,
    clear_failures as _clear_failures,
    ensure_not_locked as _ensure_not_locked,
    record_failure as _record_failure,
    subject_key as _subject_key,
)
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list

logger = logging.getLogger("app")

router = APIRouter(prefix="/auth", tags=["auth"], route_class=AuditRoute)

_USER_COLS = "id, name, phone, role, is_active, is_platform_admin, must_change_pin, created_at"
_STAFF_ROLES = ("STAFF", "TECHNICIAN")
_BOOTSTRAP_LOCK = 727001  # pg advisory lock key so two simultaneous bootstraps can't both succeed
_USER_UPDATE_LOCK = _BOOTSTRAP_LOCK + 1


def _mobile_digits(raw: str) -> str:
    """A mobile number reduced to its last 10 digits (drops spaces, dashes, '+91', a leading 0), so how it was typed
    can never make a staff / technician sign-in miss their account."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-10:] if len(digits) > 10 else digits


def _parse_account_id(raw: str) -> str | None:
    """The canonical (lower-case, hyphenated) form of a typed account ID, or None when it isn't a UUID. Lock
    counters are keyed by it, so writing the same ID in another case / format can't earn a fresh set of guesses."""
    try:
        return str(UUID(raw.strip()))
    except ValueError:
        return None


async def _target_user(conn, user_id: str):
    """The staff / technician account an owner may act on, or the HTTP error."""
    try:
        UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    target = await conn.fetchrow("SELECT role, is_platform_admin FROM users WHERE id = $1::uuid", user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return target


@router.get("/bootstrap-status")
@public_limit
async def bootstrap_status(request: Request):
    """Public: tells the app which first-run screen to show instead of a login screen nobody
    can pass yet. This is a single-tenant system, so there are exactly two bootstrap steps ever:
    the platform admin (first account), then the owner (second account, with business setup)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        admin_exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE is_platform_admin)")
        owner_exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE role = 'OWNER')")
    return {
        "needs_admin_bootstrap": not admin_exists,
        "needs_owner_bootstrap": admin_exists and not owner_exists,
    }


@router.post("/bootstrap-admin", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_setup_token)])
@credential_limit
async def bootstrap_admin(body: BootstrapRequest, request: Request):
    """The very first account, ever. Platform-admin only: no business role, no business setup
    required. Blocked the instant any account exists -- there is no other way to create one,
    and no way to create a second one. Needs the X-Setup-Token header (SETUP_TOKEN in .env)."""
    pool = get_pool()
    password_hash = await asyncio.to_thread(hash_password, body.password)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _BOOTSTRAP_LOCK)
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
            if count > 0:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Setup has already been completed")
            row = await conn.fetchrow(
                """INSERT INTO users (name, phone, password_hash, role, is_platform_admin)
                   VALUES ($1, $2, $3, 'ADMIN', TRUE) RETURNING id, name, phone, role, is_platform_admin""",
                body.name, body.phone, password_hash,
            )
    await log_event(row_to_dict(row), "BOOTSTRAP", "USER", row["id"], summary="Set up the platform admin account", request=request)
    return row_to_dict(row)


@router.post("/bootstrap-owner", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_setup_token)])
@credential_limit
async def bootstrap_owner(body: BootstrapRequest, request: Request):
    """The second account, ever -- becomes OWNER. Requires the platform admin to already exist,
    and can only ever succeed once: there is no other way to create an OWNER account, and this
    system supports exactly one. The client should route straight into business setup
    (PUT /store-profile) right after this call succeeds. Needs the X-Setup-Token header."""
    pool = get_pool()
    password_hash = await asyncio.to_thread(hash_password, body.password)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _BOOTSTRAP_LOCK)
            admin_exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE is_platform_admin)")
            if not admin_exists:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admin must be set up first")
            owner_exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE role = 'OWNER')")
            if owner_exists:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner already exists")
            row = await conn.fetchrow(
                """INSERT INTO users (name, phone, password_hash, role, is_platform_admin)
                   VALUES ($1, $2, $3, 'OWNER', FALSE) RETURNING id, name, phone, role, is_platform_admin""",
                body.name, body.phone, password_hash,
            )
    await log_event(row_to_dict(row), "BOOTSTRAP", "USER", row["id"], summary="Set up the owner account", request=request)
    return row_to_dict(row)


@router.get("/users")
async def list_users(page: Page = Depends(), _owner=Depends(require_role("OWNER"))):
    """Staff (STAFF/TECHNICIAN) accounts list. The platform admin never appears here -- it has no
    OWNER role, so it can't even call this endpoint, let alone show up in it."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT {_USER_COLS}, (avatar_key IS NOT NULL) AS has_image, avatar_updated_at AS image_updated_at,
                       COUNT(*) OVER() AS _total FROM users
                WHERE is_platform_admin = FALSE
                ORDER BY is_active DESC, role, name, id LIMIT $1 OFFSET $2""",
            page.limit, page.offset,
        )
    return paged_response(rows)


@router.get("/me")
async def me(current_user=Depends(get_current_user)):
    """The logged-in user's current profile."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {_USER_COLS} FROM users WHERE id = $1::uuid", current_user["id"])
    return row_to_dict(row)


@router.get("/team")
async def list_team(_user=Depends(get_current_user)):
    """Active users any logged-in user may pick from (e.g. as AMC visit technician). id/name/role
    plus `has_image` / `image_updated_at` (the picture itself: GET /auth/team/{id}/image). The platform admin
    never appears in this picker."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, name, role, (avatar_key IS NOT NULL) AS has_image, avatar_updated_at AS image_updated_at
               FROM users WHERE is_active = TRUE AND is_platform_admin = FALSE
               ORDER BY (role = 'TECHNICIAN') DESC, name, id"""
        )
    return rows_to_list(rows)


@router.get("/team/{user_id}/image")
async def team_member_image(user_id: str, _user=Depends(get_current_user)):
    """A teammate's profile image as a JPEG (any signed-in user). Only people the team list shows: active accounts,
    never the platform admin. 404 when the person has no image (or isn't on the team)."""
    try:
        UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    pool = get_pool()
    async with pool.acquire() as conn:
        key = await conn.fetchval(
            "SELECT avatar_key FROM users WHERE id = $1::uuid AND is_active = TRUE AND is_platform_admin = FALSE", user_id
        )
    return await avatar_util.image_response(key)


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateStaffRequest, _owner=Depends(require_role("OWNER"))):
    """Owner-only. Staff/technician accounts have no self-signup and no password: the owner
    creates the login, and a one-time 6-digit PIN is generated and returned exactly once in this
    response -- the server never stores or exposes the plaintext PIN again after this."""
    pool = get_pool()
    pin = generate_pin()
    password_hash = await asyncio.to_thread(hash_password, pin)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO users (name, phone, password_hash, role, must_change_pin)
                    VALUES ($1, $2, $3, $4, TRUE) RETURNING {_USER_COLS}""",
                body.name, _mobile_digits(body.phone) or body.phone, password_hash, body.role,
            )
        return {**row_to_dict(row), "pin": pin}
    except Exception as exc:  # asyncpg.UniqueViolationError on duplicate phone
        if "unique" in str(exc).lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone already registered")
        raise


@router.put("/users/{user_id}")
async def update_user(user_id: str, body: UpdateUserRequest, current_user=Depends(require_role("OWNER"))):
    """Rename, change role, or deactivate / reactivate a staff/technician login. Only the fields
    sent are changed. The platform admin and owner accounts can't be touched here at all -- there
    is exactly one of each, created only via the bootstrap endpoints, never via this one."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _USER_UPDATE_LOCK)
            existing = await conn.fetchrow("SELECT * FROM users WHERE id = $1::uuid", user_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if existing["is_platform_admin"] or existing["role"] not in _STAFF_ROLES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Only staff and technician accounts can be modified here",
                )

            name = body.name if body.name is not None else existing["name"]
            role = body.role if body.role is not None else existing["role"]
            is_active = body.is_active if body.is_active is not None else existing["is_active"]

            row = await conn.fetchrow(
                f"UPDATE users SET name=$1, role=$2, is_active=$3 WHERE id=$4::uuid RETURNING {_USER_COLS}",
                name, role, is_active, user_id,
            )
            if not is_active:
                # Ended for good: reactivating the account later must not bring the old sign-ins back to life.
                await revoke_user_sessions(conn, user_id)
    return row_to_dict(row)


@router.post("/users/{user_id}/reset-pin")
async def reset_pin(user_id: str, _owner=Depends(require_role("OWNER"))):
    """Owner-only. Issues a fresh one-time 6-digit PIN for a staff/technician login (invalidating
    any old sign-ins) and forces a change on next sign-in. This is the only way to recover a
    forgotten PIN -- staff/technician can only change their own PIN if they still remember it.
    The PIN is returned exactly once, here, and never again."""
    pool = get_pool()
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT role, is_platform_admin FROM users WHERE id = $1::uuid", user_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if target["is_platform_admin"] or target["role"] not in _STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only staff and technician accounts have a PIN")
        pin = generate_pin()
        password_hash = await asyncio.to_thread(hash_password, pin)
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""UPDATE users SET password_hash=$1, must_change_pin=TRUE, password_changed_at=now()
                    WHERE id=$2::uuid RETURNING {_USER_COLS}""",
                password_hash, user_id,
            )
            await revoke_user_sessions(conn, user_id)
    return {**row_to_dict(row), "pin": pin}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_sessions(user_id: str, _owner=Depends(require_role("OWNER"))):
    """Owner-only. Signs a staff / technician member out of every device right now, without touching their PIN
    (use reset-pin when the PIN may be known to someone else, deactivate to stop them signing in at all)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        target = await _target_user(conn, user_id)
        if target["is_platform_admin"] or target["role"] not in _STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only staff and technician accounts can be signed out here")
        ended = await revoke_user_sessions(conn, user_id)
    return {"message": "Signed out of every device", "sessions_ended": ended}


@router.post("/logout")
async def logout(current_user=Depends(get_current_user)):
    """Ends THIS sign-in: the token stops working immediately, on the server. Other devices stay signed in."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE auth_sessions SET revoked_at = now() WHERE id = $1::uuid AND revoked_at IS NULL",
            current_user["session_id"],
        )
    return {"message": "Signed out"}


@router.post("/logout-all")
async def logout_all(current_user=Depends(get_current_user)):
    """Ends every sign-in of this account, this device included ("sign out everywhere")."""
    pool = get_pool()
    async with pool.acquire() as conn:
        ended = await revoke_user_sessions(conn, current_user["id"])
    return {"message": "Signed out of every device", "sessions_ended": ended}


@router.post("/change-pin")
@credential_limit
async def change_pin(body: ChangePinRequest, request: Request, current_user=Depends(get_current_user)):
    """Self-service, staff/technician only. Requires the current PIN -- if it's forgotten, only
    the owner can recover access, via reset-pin. Signs the caller in again with a fresh token
    (the old one stops working, same as any other credential change).

    Wrong current PINs count toward the same lock as sign-in (per account and client address): after
    LOGIN_MAX_FAILURES in a row from one address the answer is 429 even for the right PIN, and the 6-digit PIN's daily
    ceiling applies too. Anyone holding a stolen token can therefore not use this endpoint to guess the PIN."""
    if current_user["role"] not in _STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only staff and technician accounts use a PIN")
    pool = get_pool()
    ip = client_ip(request)
    subject = str(current_user["id"])
    async with pool.acquire() as conn:
        await _ensure_not_locked(conn, subject, current_user, ip, daily_ceiling=settings.login_pin_daily_failure_ceiling)
        existing = await conn.fetchrow("SELECT password_hash FROM users WHERE id = $1::uuid", current_user["id"])
    # (verified with no connection held, so a wrong guess can record itself on its own connection)
    if existing is None or not await asyncio.to_thread(verify_password, body.current_pin, existing["password_hash"]):
        await _record_failure(subject, current_user, ip, request, during="change-pin")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current PIN is incorrect")
    if body.new_pin == body.current_pin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a different PIN from the current one")
    new_hash = await asyncio.to_thread(hash_password, body.new_pin)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""UPDATE users SET password_hash=$1, must_change_pin=FALSE, password_changed_at=now()
                    WHERE id=$2::uuid RETURNING {_USER_COLS}""",
                new_hash, current_user["id"],
            )
            await revoke_user_sessions(conn, current_user["id"])  # every other device is signed out
            token = await issue_session_token(conn, str(row["id"]), row["name"], row["role"], request)
        await _clear_failures(conn, subject, ip)
    return {"token": token, "user": row_to_dict(row)}


@router.post("/login")
@credential_limit
async def login(body: LoginRequest, request: Request):
    """Owner / platform admin only, by phone + password. Staff and technician accounts have no
    password and must use /login-pin instead."""
    pool = get_pool()
    ip = client_ip(request)
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE phone = $1 AND is_active = TRUE AND role NOT IN ('STAFF', 'TECHNICIAN')",
            body.phone,
        )
        subject = str(user["id"]) if user else _subject_key(body.phone)
        await _ensure_not_locked(conn, subject, user, ip)

    if user is not None:
        valid = await asyncio.to_thread(verify_password, body.password, user["password_hash"])
    else:
        await asyncio.to_thread(burn_password_check, body.password)  # an unknown phone takes as long as a wrong password
        valid = False
    if not valid:
        await _record_failure(subject, user, ip, request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    async with pool.acquire() as conn:
        token = await issue_session_token(conn, str(user["id"]), user["name"], user["role"], request)
        await _clear_failures(conn, subject, ip)
    await log_event(_actor(user), "LOGIN", "USER", user["id"], request=request)
    return {
        "token": token,
        "user": {
            "id": str(user["id"]), "name": user["name"], "role": user["role"], "phone": user["phone"],
            "is_platform_admin": user["is_platform_admin"],
        },
    }


@router.post("/login-pin")
@credential_limit
async def login_pin(body: LoginPinRequest, request: Request):
    """Staff / technician only, by mobile number + PIN (the phone the owner registered the login with).
    The account ID (their user ID) is still accepted instead of the phone, so older app versions and
    saved quick sign-ins keep working. No self-signup exists for these roles."""
    pool = get_pool()
    ip = client_ip(request)
    phone = _mobile_digits(body.phone) if body.phone else ""
    account_id = None if phone else _parse_account_id(body.account_id or "")  # None when it isn't a UUID
    async with pool.acquire() as conn:
        user = None
        if phone:
            user = await conn.fetchrow(
                """SELECT * FROM users WHERE right(regexp_replace(phone, '\\D', '', 'g'), 10) = $1
                   AND role IN ('STAFF', 'TECHNICIAN') AND is_active = TRUE""",
                phone,
            )
            # Lock counters follow the account once it is known; an unknown number is counted by the number itself.
            subject = str(user["id"]) if user else _subject_key(phone)
        else:
            if account_id is not None:
                user = await conn.fetchrow(
                    "SELECT * FROM users WHERE id = $1::uuid AND role IN ('STAFF', 'TECHNICIAN') AND is_active = TRUE",
                    account_id,
                )
            subject = account_id or _subject_key(body.account_id or "")
        await _ensure_not_locked(conn, subject, user, ip, daily_ceiling=settings.login_pin_daily_failure_ceiling)

    if user is not None:
        valid = await asyncio.to_thread(verify_password, body.pin, user["password_hash"])
    else:
        await asyncio.to_thread(burn_password_check, body.pin)  # an unknown account ID takes as long as a wrong PIN
        valid = False
    if not valid:
        await _record_failure(subject, user, ip, request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid mobile number or PIN")

    async with pool.acquire() as conn:
        token = await issue_session_token(conn, str(user["id"]), user["name"], user["role"], request)
        await _clear_failures(conn, subject, ip)
    await log_event(_actor(user), "LOGIN", "USER", user["id"], request=request)
    return {
        "token": token,
        "must_change_pin": user["must_change_pin"],
        "user": {
            "id": str(user["id"]), "name": user["name"], "role": user["role"], "phone": user["phone"],
            "is_platform_admin": False,
        },
    }
