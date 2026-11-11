import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from passlib.context import CryptContext

from app.config import settings
from app.db import get_pool
from app.utils.audit import client_ip

# The only endpoints a one-time-PIN session may call: change the PIN, read its own account (to show the right
# screen), and sign out.
_PIN_CHANGE_PATHS = ("/auth/change-pin", "/auth/me", "/auth/logout")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def password_policy_error(password: str) -> str | None:
    """Why a NEW owner / admin password is too weak, or None when it is fine. Only applied when a password is
    chosen (change-password); existing passwords keep working. The length floor (8) is enforced by the schema."""
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Use at least one letter and one number in the new password"
    if len(set(password)) < 4:
        return "Choose a less repetitive password"
    return None


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def generate_pin() -> str:
    """A random 6-digit login PIN for staff/technician accounts (hashed with hash_password before
    storage, same as a password). Zero-padded, so e.g. 000482 is a valid PIN."""
    return f"{secrets.randbelow(1_000_000):06d}"


# A real bcrypt hash of a throw-away password, made with the same settings as every stored hash. Checking a login
# against it when the account doesn't exist makes "unknown phone" cost the same time as "wrong password", so response
# time can't be used to find out which phone numbers are registered.
_DUMMY_PASSWORD_HASH = pwd_context.hash(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """Spend the time of one password check without a real account to check against."""
    try:
        pwd_context.verify(password, _DUMMY_PASSWORD_HASH)
    except Exception:
        pass


def create_access_token(user_id: str, name: str, role: str, session_id: str, expires_at: datetime) -> str:
    """A signed token that is only good while auth_sessions row `session_id` is still active."""
    payload = {
        "id": user_id, "name": name, "role": role, "sid": session_id,
        "exp": expires_at, "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        # Tokens issued before sessions existed have no "sid" and are refused: they could never be revoked.
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "iat", "sid", "id"]})
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


async def issue_session_token(conn, user_id: str, name: str, role: str, request: Request | None = None) -> str:
    """Start a new sign-in (one auth_sessions row) and return the token for it. Call inside the caller's
    transaction when the credential change that made the sign-in must stay atomic with it."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expires_minutes)
    agent = (request.headers.get("user-agent") if request is not None else None) or None
    session_id = await conn.fetchval(
        """INSERT INTO auth_sessions (user_id, expires_at, ip_address, user_agent)
           VALUES ($1::uuid, $2, $3, $4) RETURNING id""",
        str(user_id), expires_at, client_ip(request), agent[:200] if agent else None,
    )
    # Housekeeping: sessions that ended more than a week ago are of no use any more.
    await conn.execute(
        "DELETE FROM auth_sessions WHERE expires_at < now() - interval '7 days' OR revoked_at < now() - interval '7 days'"
    )
    return create_access_token(str(user_id), name, role, str(session_id), expires_at)


async def revoke_user_sessions(conn, user_id: str, except_session_id: str | None = None) -> int:
    """End every active sign-in of an account (optionally keeping one). Returns how many were ended."""
    result = await conn.execute(
        """UPDATE auth_sessions SET revoked_at = now()
           WHERE user_id = $1::uuid AND revoked_at IS NULL AND ($2::uuid IS NULL OR id <> $2::uuid)""",
        str(user_id), except_session_id,
    )
    return int(result.rsplit(" ", 1)[-1])


def _as_uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any]:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth token")
    payload = decode_access_token(credentials.credentials)
    user_id = _as_uuid(payload.get("id"))
    session_id = _as_uuid(payload.get("sid"))

    # Check the sign-in is still active, the account still exists / is active and that the password wasn't reset
    # after this token was issued -- otherwise a signed-out, deactivated or reset user would keep access until expiry.
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """SELECT u.id, u.name, u.role, u.is_active, u.is_platform_admin, u.must_change_pin, u.password_changed_at,
                      s.id AS session_id, s.expires_at AS session_expires_at, s.revoked_at AS session_revoked_at
               FROM users u LEFT JOIN auth_sessions s ON s.id = $2::uuid AND s.user_id = u.id
               WHERE u.id = $1::uuid""",
            user_id, session_id,
        )
    if user is None or not user["is_active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is disabled")
    if (
        user["session_id"] is None
        or user["session_revoked_at"] is not None
        or user["session_expires_at"] <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signed out — please log in again")
    changed_at = user["password_changed_at"]
    if changed_at is not None:
        iat = payload.get("iat")
        if iat is None or int(iat) < int(changed_at.timestamp()):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Password changed — please log in again")
    # A one-time PIN (new account / owner reset) may only be used to change it. Without this a staff login could
    # keep working on the PIN the owner generated, which the owner (and anyone who saw the screen) knows.
    if user["must_change_pin"] and not request.url.path.rstrip("/").endswith(_PIN_CHANGE_PATHS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must change your PIN before you can continue",
            headers={"X-Must-Change-Pin": "true"},
        )
    current = {
        "id": str(user["id"]),
        "name": user["name"],
        "role": user["role"],
        "is_platform_admin": user["is_platform_admin"],
        "session_id": session_id,
    }
    request.state.user = current  # read by the audit log (app/utils/audit.py) once the request has been handled
    return current


def require_role(*roles: str):
    async def dependency(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if current_user.get("role") not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user

    return dependency


async def require_platform_admin(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """The platform admin is the very first account registered, has no OWNER role, and so has no
    access to business endpoints already. Only it may back up / restore the database."""
    if not current_user.get("is_platform_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the platform admin can do this")
    return current_user


async def require_setup_token(x_setup_token: str | None = Header(default=None)) -> None:
    """Guards the two first-run endpoints. The token comes from SETUP_TOKEN in the server's .env, so only
    someone with server access can create the first accounts. Disabled (403) when SETUP_TOKEN is unset."""
    expected = settings.setup_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="First-run setup is disabled: set SETUP_TOKEN in the server's .env and restart the API",
        )
    if not x_setup_token or not secrets.compare_digest(x_setup_token.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid setup token")
