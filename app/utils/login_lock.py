"""Brute-force guard shared by every endpoint that checks a PIN / password (v1.17.3).

Sign-in (/auth/login, /auth/login-pin) and the credential changes (/auth/change-pin, /profile/change-password) all
count wrong guesses against the SAME subject (the account) and the same client address, and all refuse while that is
locked. Before 1.17.3 only sign-in did: a stolen session token could be used to test the current PIN / password
through the change endpoints, limited only by the per-IP request rate.

(The text below is the former body of app/routers/auth.py, moved here unchanged apart from the `during` note.)
"""
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status

from app.config import settings
from app.db import get_pool
from app.utils.audit import log_event

logger = logging.getLogger("app")


def actor(user) -> dict | None:
    """Who a sign-in attempt is attributed to in the audit log (None = no such account)."""
    return {"id": user["id"], "name": user["name"], "role": user["role"]} if user is not None else None


def subject_key(typed: str) -> str:
    """What a failed sign-in is counted under when the typed phone / account ID belongs to no account: a keyed hash
    of the text, never the text itself (it may be a password typed into the wrong box). The same text always maps
    to the same key, so unknown subjects are locked exactly like real ones."""
    digest = hmac.new(settings.jwt_secret.encode(), typed.strip().encode(), hashlib.sha256).hexdigest()
    return "t:" + digest[:32]


# Failed sign-ins are counted per "subject" in login_failures (short-lived, pruned after 25 hours -- NOT in the
# append-only audit log): the account's ID when the typed phone / ID belongs to an account, a keyed hash of the
# typed text otherwise. Unknown subjects are locked exactly like real accounts, so neither the response nor its
# timing shows whether a phone number or account ID is registered.
_LOCK_SQL = """
WITH recent AS (
    SELECT created_at, ip_address FROM login_failures
    WHERE subject = $1 AND created_at > now() - make_interval(mins => $3)
), mine AS (
    SELECT created_at FROM recent WHERE ip_address IS NOT DISTINCT FROM $2::varchar
), day AS (
    SELECT created_at FROM login_failures
    WHERE subject = $1 AND created_at > now() - interval '24 hours'
)
SELECT (SELECT COUNT(*) FROM mine) AS ip_fails,
       (SELECT MAX(created_at) FROM mine) AS ip_last_fail,
       (SELECT COUNT(*) FROM recent) AS all_fails,
       (SELECT MAX(created_at) FROM recent) AS all_last_fail,
       (SELECT COUNT(*) FROM day) AS day_fails,
       (SELECT created_at FROM day ORDER BY created_at DESC OFFSET GREATEST($6::int, 1) - 1 LIMIT 1) AS day_edge,
       EXISTS (SELECT 1 FROM audit_logs WHERE action = 'LOGIN' AND user_id = $4::uuid
                 AND ip_address IS NOT DISTINCT FROM $2::varchar
                 AND created_at > now() - make_interval(days => $5)) AS known_ip
"""


def wait_text(wait: int) -> str:
    minutes = -(-wait // 60)
    return f"{-(-minutes // 60)} hour(s)" if minutes >= 120 else f"{minutes} minute(s)"


async def ensure_not_locked(conn, subject: str, user, ip: str | None, daily_ceiling: int = 0) -> None:
    """Brute-force guard: refuse sign-in (429) while this account has had `login_max_failures` wrong credentials in
    a row FROM THIS CLIENT ADDRESS. Checked BEFORE the password / PIN is verified, so a correct guess during the
    lockout is refused too.

    The lock is per (account, client IP), not per account: otherwise anyone who knows the owner's phone number could
    keep the owner locked out forever with a script. Whoever is guessing locks only their own address.
    To still stop guessing spread over many addresses: once the account has `login_account_failure_ceiling` failures
    from all addresses together inside the window, addresses that haven't signed in to it successfully in the last
    `login_known_ip_days` days are refused as well (the owner's usual addresses keep working). `daily_ceiling` does
    the same over 24 hours (used for the 6-digit staff PINs, which are small enough to be worth guessing slowly).

    Failures come from login_failures (rows since the last successful sign-in from that address, no older than the
    window); attempts made while locked are not counted, so the lock ends `login_lockout_minutes` after the last
    counted failure instead of extending forever."""
    row = await conn.fetchrow(
        _LOCK_SQL, subject, ip, settings.login_lockout_minutes, str(user["id"]) if user is not None else None,
        settings.login_known_ip_days, daily_ceiling,
    )
    window = timedelta(minutes=settings.login_lockout_minutes)
    until = None
    if row["ip_fails"] >= settings.login_max_failures:
        until = row["ip_last_fail"] + window
    elif row["all_fails"] >= settings.login_account_failure_ceiling and not row["known_ip"]:
        until = row["all_last_fail"] + window
    elif daily_ceiling > 0 and row["day_fails"] >= daily_ceiling and not row["known_ip"]:
        until = row["day_edge"] + timedelta(hours=24)
    if until is not None:
        wait = max(int((until - datetime.now(timezone.utc)).total_seconds()), 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed sign-in attempts. Try again in {wait_text(wait)}.",
            headers={"Retry-After": str(wait)},
        )


async def record_failure(subject: str, user, ip: str | None, request: Request, during: str | None = None) -> None:
    """Counts one wrong credential. Only a failure against a REAL account may reach the audit log, and only the
    first of a streak and the one that locks the address (so guessing can't flood the append-only log); failures
    against unknown phones / IDs are counted here and nowhere else. Never raises: a bookkeeping error must not
    turn a plain 401 into a 500. `during` names the endpoint when the wrong credential was typed anywhere other than
    the sign-in screens ("change-pin", "change-password"): it counts toward the very same lock."""
    try:
        async with get_pool().acquire() as conn:
            await conn.execute("INSERT INTO login_failures (subject, ip_address) VALUES ($1, $2)", subject, ip)
            await conn.execute("DELETE FROM login_failures WHERE created_at < now() - interval '25 hours'")
            attempts = await conn.fetchval(
                """SELECT COUNT(*) FROM login_failures WHERE subject = $1 AND ip_address IS NOT DISTINCT FROM $2::varchar
                   AND created_at > now() - make_interval(mins => $3)""",
                subject, ip, settings.login_lockout_minutes,
            )
    except Exception:
        logger.exception("could not record a failed sign-in")
        return
    if user is not None and attempts in (1, settings.login_max_failures):
        await log_event(
            actor(user), "LOGIN_FAILED", "USER", user["id"], request=request,
            details={"attempts": attempts, "locked": attempts >= settings.login_max_failures,
                     **({"during": during} if during else {})},
        )


async def clear_failures(conn, subject: str, ip: str | None) -> None:
    """A successful sign-in wipes that address's failure count for the account."""
    await conn.execute(
        "DELETE FROM login_failures WHERE subject = $1 AND ip_address IS NOT DISTINCT FROM $2::varchar", subject, ip
    )
