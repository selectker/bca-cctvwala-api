"""Server-side sessions: logout, sign out everywhere, owner revoking a staff member, credential changes (end to end
against a real PostgreSQL, same setup as test_api.py). Runs on an empty database or after the other modules."""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.limiter import limiter
from app.main import app
from tests.helpers import first_login, raw_connect

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
NEW_PIN = "482916"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        limiter.reset()
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


@pytest.fixture()
def owner_token(client):
    """A fresh owner sign-in per test (one test changes the owner's password, which ends every sign-in)."""
    state = ok(client.get("/api/v1/auth/bootstrap-status"))
    if state["needs_admin_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}), 201)
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_owner_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}), 201)
    return ok(client.post("/api/v1/auth/login", json=OWNER))["token"]


def new_staff(client, owner_token, label):
    phone = f"95{str(time.time_ns())[-8:]}"
    return ok(client.post("/api/v1/auth/users", headers=H(owner_token),
                          json={"name": f"Sess {label}", "phone": phone, "role": "STAFF"}), 201)


def sign_in(client, staff, pin=NEW_PIN):
    return ok(client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": pin}))["token"]


def alive(client, token):
    return client.get("/api/v1/auth/me", headers=H(token)).status_code == 200


def session_id(token):
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["sid"]


def test_logout_ends_only_this_sign_in(client, owner_token):
    a = ok(client.post("/api/v1/auth/login", json=OWNER))["token"]
    b = ok(client.post("/api/v1/auth/login", json=OWNER))["token"]
    assert alive(client, a) and alive(client, b)
    assert ok(client.post("/api/v1/auth/logout", headers=H(a)))["message"] == "Signed out"
    assert client.get("/api/v1/auth/me", headers=H(a)).status_code == 401  # the token itself is still validly signed
    assert alive(client, b) and alive(client, owner_token)
    assert client.post("/api/v1/auth/logout", headers=H(a)).status_code == 401  # nothing left to sign out
    assert client.post("/api/v1/auth/logout").status_code == 401
    rows = ok(client.get("/api/v1/audit-logs", headers=H(owner_token), params={"action": "LOGOUT"}))
    assert rows and rows[0]["summary"] == "Signed out"


def test_logout_all_signs_out_every_device(client, owner_token):
    staff = new_staff(client, owner_token, "all")
    first = first_login(client, staff, NEW_PIN)
    second, third = sign_in(client, staff), sign_in(client, staff)
    result = ok(client.post("/api/v1/auth/logout-all", headers=H(second)))
    assert result["sessions_ended"] >= 3
    assert not any(alive(client, t) for t in (first, second, third))
    assert alive(client, sign_in(client, staff))  # signing in again works: the PIN is untouched


def test_owner_can_end_a_staff_members_sessions(client, owner_token):
    staff = new_staff(client, owner_token, "revoked")
    token = first_login(client, staff, NEW_PIN)
    assert alive(client, token)
    ended = ok(client.post(f"/api/v1/auth/users/{staff['id']}/revoke-sessions", headers=H(owner_token)))
    assert ended["sessions_ended"] >= 1 and not alive(client, token)
    assert alive(client, sign_in(client, staff))
    # owner only; staff only
    assert client.post(f"/api/v1/auth/users/{staff['id']}/revoke-sessions", headers=H(sign_in(client, staff))).status_code == 403
    me = ok(client.get("/api/v1/auth/me", headers=H(owner_token)))
    assert client.post(f"/api/v1/auth/users/{me['id']}/revoke-sessions", headers=H(owner_token)).status_code == 400
    assert client.post("/api/v1/auth/users/not-a-uuid/revoke-sessions", headers=H(owner_token)).status_code == 404


def test_changing_the_pin_signs_out_the_other_devices(client, owner_token):
    staff = new_staff(client, owner_token, "pin")
    phone = first_login(client, staff, NEW_PIN)
    tablet = sign_in(client, staff)
    changed = ok(client.post("/api/v1/auth/change-pin", headers=H(tablet), json={"current_pin": NEW_PIN, "new_pin": "135791"}))
    assert not alive(client, phone) and not alive(client, tablet)
    assert alive(client, changed["token"])  # the device that changed it stays signed in


def test_reset_pin_and_deactivation_end_sessions_for_good(client, owner_token):
    staff = new_staff(client, owner_token, "gone")
    token = first_login(client, staff, NEW_PIN)
    ok(client.put(f"/api/v1/auth/users/{staff['id']}", headers=H(owner_token), json={"is_active": False}))
    assert not alive(client, token)
    ok(client.put(f"/api/v1/auth/users/{staff['id']}", headers=H(owner_token), json={"is_active": True}))
    assert not alive(client, token)  # reactivating must not bring the old sign-in back

    token2 = sign_in(client, staff)
    reset = ok(client.post(f"/api/v1/auth/users/{staff['id']}/reset-pin", headers=H(owner_token)))
    assert not alive(client, token2) and reset["pin"]


def test_changing_the_owner_password_signs_out_other_devices(client, owner_token):
    other = ok(client.post("/api/v1/auth/login", json=OWNER))["token"]
    changed = ok(client.post("/api/v1/profile/change-password", headers=H(other),
                             json={"current_password": OWNER["password"], "new_password": "ownerpass1-tmp"}))
    try:
        assert not alive(client, owner_token) and not alive(client, other)
        assert alive(client, changed["token"])
    finally:  # put the password back for the other test modules
        back = ok(client.post("/api/v1/profile/change-password", headers=H(changed["token"]),
                              json={"current_password": "ownerpass1-tmp", "new_password": OWNER["password"]}))
        assert alive(client, back["token"])


def test_one_time_pin_session_can_still_sign_out(client, owner_token):
    staff = new_staff(client, owner_token, "onetime")
    token = ok(client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": staff["pin"]}))["token"]
    assert client.get("/api/v1/auth/team", headers=H(token)).status_code == 403  # must change the PIN first
    ok(client.post("/api/v1/auth/logout", headers=H(token)))
    assert client.get("/api/v1/auth/me", headers=H(token)).status_code == 401


def test_tokens_without_a_live_session_are_refused(client, owner_token):
    me = ok(client.get("/api/v1/auth/me", headers=H(owner_token)))
    now = datetime.now(timezone.utc)
    base = {"id": me["id"], "name": "x", "role": "OWNER", "iat": now, "exp": now + timedelta(days=1)}
    sign = lambda payload: jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    assert client.get("/api/v1/auth/me", headers=H(sign(base))).status_code == 401                       # old-style token: no session id
    assert client.get("/api/v1/auth/me", headers=H(sign({**base, "sid": "not-a-uuid"}))).status_code == 401
    ghost = "00000000-0000-4000-8000-000000000000"
    assert client.get("/api/v1/auth/me", headers=H(sign({**base, "sid": ghost}))).status_code == 401      # signed, but no such session
    assert client.get("/api/v1/auth/me", headers=H(jwt.encode({**base, "sid": session_id(owner_token)}, "x" * 40, algorithm="HS256"))).status_code == 401


def test_an_expired_session_is_refused_even_though_the_token_is_not(client, owner_token):
    token = ok(client.post("/api/v1/auth/login", json=OWNER))["token"]
    assert alive(client, token)

    async def expire():
        conn = await raw_connect()
        try:
            await conn.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 minute' WHERE id = $1::uuid", session_id(token))
        finally:
            await conn.close()

    asyncio.run(expire())
    assert client.get("/api/v1/auth/me", headers=H(token)).status_code == 401
