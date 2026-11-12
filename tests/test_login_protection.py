"""Brute-force protection on sign-in, end to end against a real PostgreSQL (same setup as test_api.py).

Runs on an empty database or after the other test modules (it bootstraps the admin / owner only when they
don't exist yet, with the same credentials they use).
"""
import asyncio
import os
import random
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import security
from app.config import settings
from app.limiter import limiter
from app.main import app
from tests.helpers import raw_connect

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}


_MAIN_PORTAL = {}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        _MAIN_PORTAL["portal"] = c.portal
        yield c
    _MAIN_PORTAL.clear()


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


@pytest.fixture(scope="module")
def owner_token(client):
    state = ok(client.get("/api/v1/auth/bootstrap-status"))
    if state["needs_admin_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}), 201)
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_owner_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}), 201)
    return ok(client.post("/api/v1/auth/login", json=OWNER))["token"]


def new_staff(client, owner_token, label):
    phone = f"96{str(time.time_ns())[-8:]}"
    return ok(client.post("/api/v1/auth/users", headers=H(owner_token),
                          json={"name": f"LP {label}", "phone": phone, "role": "STAFF"}), 201)


def pin_login(client, staff, pin=None):
    return client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": pin or staff["pin"]})


def wrong_pin(staff):
    return "000000" if staff["pin"] != "000000" else "111111"


def test_account_locks_after_repeated_failures(client, owner_token):
    limiter.reset()
    victim = new_staff(client, owner_token, "victim")
    bystander = new_staff(client, owner_token, "bystander")

    for _ in range(settings.login_max_failures):
        assert pin_login(client, victim, wrong_pin(victim)).status_code == 401

    # Locked: even the CORRECT PIN is refused, with a Retry-After hint and the usual {"error": ...} shape.
    r = pin_login(client, victim)
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) > 0 and "error" in r.json()
    # ...and so is another wrong guess (it must not be evaluated at all).
    assert pin_login(client, victim, wrong_pin(victim)).status_code == 429

    # One account locked doesn't affect anyone else.
    ok(pin_login(client, bystander))
    ok(client.post("/api/v1/auth/login", json=OWNER))

    # The lock expires (simulate the window having passed instead of waiting 15 minutes).
    old = settings.login_lockout_minutes
    settings.login_lockout_minutes = 0
    try:
        ok(pin_login(client, victim))
    finally:
        settings.login_lockout_minutes = old


def test_successful_login_resets_the_counter(client, owner_token):
    limiter.reset()
    user = new_staff(client, owner_token, "reset")
    for _ in range(settings.login_max_failures - 1):
        assert pin_login(client, user, wrong_pin(user)).status_code == 401
    ok(pin_login(client, user))  # still allowed: one short of the limit -> counter restarts
    for _ in range(settings.login_max_failures - 1):
        assert pin_login(client, user, wrong_pin(user)).status_code == 401
    ok(pin_login(client, user))  # again fine, so failures were NOT carried over


def test_owner_password_login_locks_too(client, owner_token):
    limiter.reset()
    # A throwaway owner-style account isn't creatable (only one owner), so use the platform admin account.
    for _ in range(settings.login_max_failures):
        assert client.post("/api/v1/auth/login", json={**ADMIN, "password": "not-the-password"}).status_code == 401
    assert client.post("/api/v1/auth/login", json=ADMIN).status_code == 429
    old = settings.login_lockout_minutes
    settings.login_lockout_minutes = 0
    try:
        ok(client.post("/api/v1/auth/login", json=ADMIN))  # also writes a LOGIN row, clearing the counter
    finally:
        settings.login_lockout_minutes = old


def test_unknown_accounts_are_ip_throttled(client, monkeypatch):
    limiter.reset()
    monkeypatch.setattr(settings, "login_rate_limit", "3/minute")
    for _ in range(3):
        assert client.post("/api/v1/auth/login", json={"phone": "9999999999", "password": "whatever1"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"phone": "9999999999", "password": "whatever1"})
    assert r.status_code == 429 and "error" in r.json()
    # The bucket is shared by every credential endpoint, so switching endpoint doesn't get a fresh allowance.
    assert client.post("/api/v1/auth/login-pin", json={"account_id": "00000000-0000-0000-0000-000000000000",
                                                    "pin": "123456"}).status_code == 429
    monkeypatch.undo()
    limiter.reset()


def other_address():
    """A client that appears to come from a different (random, so runs don't share state) IP address."""
    ip = f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
    other = TestClient(app, client=(ip, 50000))  # no `with`: the app is already running through the `client` fixture
    # Run its requests on the SAME event loop as the `client` fixture. Without this every request starts its own loop
    # in a new thread, and the asyncpg pool (created on the fixture's loop) is then used from a loop it doesn't belong
    # to, which crashes the interpreter (segfault inside asyncpg) instead of failing cleanly.
    other.portal = _MAIN_PORTAL["portal"]
    return other


def test_guessing_from_one_address_cannot_lock_the_real_owner_out(client, owner_token, monkeypatch):
    limiter.reset()
    attacker = other_address()
    for _ in range(settings.login_max_failures):
        assert attacker.post("/api/v1/auth/login", json={**OWNER, "password": "not-the-password"}).status_code == 401
    # The attacker's address is locked, even for the right password...
    assert attacker.post("/api/v1/auth/login", json=OWNER).status_code == 429
    # ...but the owner, signing in from their own address, is not affected at all, and neither are the staff.
    ok(client.post("/api/v1/auth/login", json=OWNER))
    # (clean up: let the attacker address in again so a re-run on the same database starts clean)
    monkeypatch.setattr(settings, "login_lockout_minutes", 0)
    ok(attacker.post("/api/v1/auth/login", json=OWNER))


def test_unknown_and_known_accounts_are_locked_the_same_way(client, owner_token):
    """A lock-out that only exists for real accounts would reveal which phone numbers / account IDs are registered."""
    limiter.reset()
    me = other_address()
    unknown_phone = {"phone": f"98{time.time_ns() % 10**8:08d}", "password": "whatever1"}
    for _ in range(settings.login_max_failures):
        assert me.post("/api/v1/auth/login", json=unknown_phone).status_code == 401
    r = me.post("/api/v1/auth/login", json=unknown_phone)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and "error" in r.json()

    ghost = str(uuid.uuid4())
    for variant in (ghost, ghost.upper(), "{" + ghost + "}", ghost.replace("-", "")):
        for _ in range(2):  # writing the same ID in another format doesn't earn a fresh set of guesses
            me.post("/api/v1/auth/login-pin", json={"account_id": variant, "pin": "123456"})
    assert me.post("/api/v1/auth/login-pin", json={"account_id": ghost.upper(), "pin": "123456"}).status_code == 429


def test_many_addresses_cannot_guess_an_account_but_its_known_address_still_works(client, owner_token, monkeypatch):
    limiter.reset()
    staff = new_staff(client, owner_token, "spread")
    ok(pin_login(client, staff))  # this address now counts as "known" for the account
    monkeypatch.setattr(settings, "login_max_failures", 100)  # isolate the account-wide ceiling
    monkeypatch.setattr(settings, "login_account_failure_ceiling", 3)
    for _ in range(3):  # one guess from each of three different addresses
        assert pin_login(other_address(), staff, wrong_pin(staff)).status_code == 401
    # an address that never signed in to this account is now refused, even with the right PIN...
    assert pin_login(other_address(), staff).status_code == 429
    # ...the account's usual address is not.
    ok(pin_login(client, staff))


def test_unknown_phone_costs_a_password_check_too(client, monkeypatch):
    """Response time must not reveal which phones are registered: every failed login does one bcrypt check."""
    limiter.reset()
    calls = []
    monkeypatch.setattr("app.routers.auth.burn_password_check", lambda pw: calls.append(pw))
    me = other_address()
    assert me.post("/api/v1/auth/login", json={"phone": f"97{time.time_ns() % 10**8:08d}", "password": "whatever1"}).status_code == 401
    assert me.post("/api/v1/auth/login-pin", json={"account_id": str(uuid.uuid4()), "pin": "123456"}).status_code == 401
    assert me.post("/api/v1/auth/login-pin", json={"account_id": "not-a-uuid", "pin": "123456"}).status_code == 401
    assert len(calls) == 3  # the stand-in check ran for each unknown account, valid UUID or not


def test_stand_in_hash_has_the_same_cost_as_real_hashes():
    real = security.hash_password("some password")
    assert security._DUMMY_PASSWORD_HASH.split("$")[:3] == real.split("$")[:3]  # same bcrypt version and rounds
    assert security.verify_password("some password", real)
    security.burn_password_check("anything")  # never raises


def db(sql, *args):
    """Run one statement straight against the test database; returns the rows (if any)."""
    async def run():
        conn = await raw_connect()
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_failed_sign_ins_stay_out_of_the_audit_log_and_what_was_typed_is_not_kept(client, owner_token):
    limiter.reset()
    me = other_address()
    typed = f"not-a-phone-{time.time_ns()}"
    for _ in range(3):
        assert me.post("/api/v1/auth/login", json={"phone": typed, "password": "whatever1"}).status_code == 401
    subjects = [r["subject"] for r in db("SELECT subject FROM login_failures")]
    assert subjects and all(typed not in s for s in subjects)       # only a keyed hash of it is kept, and only here
    assert not db("SELECT 1 FROM audit_logs WHERE details::text LIKE $1", f"%{typed}%")
    assert not db("SELECT 1 FROM audit_logs WHERE action = 'LOGIN_FAILED' AND user_id IS NULL AND created_at > now() - interval '5 minutes'")

    # Against a real account only the first failure of a streak and the one that locks the address are audited.
    staff = new_staff(client, owner_token, "audit")
    for _ in range(settings.login_max_failures):
        assert pin_login(me, staff, wrong_pin(staff)).status_code == 401
    rows = db("SELECT details FROM audit_logs WHERE action = 'LOGIN_FAILED' AND user_id = $1::uuid", staff["id"])
    assert len(rows) == 2


def test_old_failure_rows_are_pruned(client):
    limiter.reset()
    db("INSERT INTO login_failures (subject, ip_address, created_at) VALUES ('t:ancient', '203.0.113.9', now() - interval '26 hours')")
    assert other_address().post("/api/v1/auth/login", json={"phone": "9999999990", "password": "whatever1"}).status_code == 401
    assert not db("SELECT 1 FROM login_failures WHERE subject = 't:ancient'")


def test_pin_accounts_have_a_daily_ceiling_across_all_addresses(client, owner_token, monkeypatch):
    """A 6-digit PIN is small enough to be worth guessing slowly from many addresses for months."""
    limiter.reset()
    staff = new_staff(client, owner_token, "daily")
    ok(pin_login(client, staff))  # this address is now "known" to the account
    monkeypatch.setattr(settings, "login_max_failures", 100)             # isolate the 24-hour ceiling
    monkeypatch.setattr(settings, "login_account_failure_ceiling", 1000)
    monkeypatch.setattr(settings, "login_pin_daily_failure_ceiling", 3)
    for _ in range(3):
        assert pin_login(other_address(), staff, wrong_pin(staff)).status_code == 401
    r = pin_login(other_address(), staff)  # a new address, even with the right PIN, waits for the 24 hours to roll on
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 23 * 3600, r.text
    ok(pin_login(client, staff))           # the account's usual address is not affected
    ok(other_address().post("/api/v1/auth/login", json=OWNER))  # and the owner's password login has no such ceiling


# ---------------- v1.17.3: the credential-change endpoints share the sign-in lock ----------------
def _fresh_staff_session(client, owner_token, label):
    from tests.helpers import first_login

    staff = new_staff(client, owner_token, label)
    token = first_login(client, staff, new_pin="482916")
    return staff, token, "482916"


def test_change_pin_cannot_be_used_to_guess_the_pin(client, owner_token):
    limiter.reset()
    staff, token, pin = _fresh_staff_session(client, owner_token, "changepin")
    bad = "000000" if pin != "000000" else "111111"
    attempt = lambda current: client.post("/api/v1/auth/change-pin", headers=H(token),
                                          json={"current_pin": current, "new_pin": "913572"})

    for _ in range(settings.login_max_failures):
        assert attempt(bad).status_code == 401
    # Locked: even the CORRECT current PIN is refused and the PIN is unchanged...
    r = attempt(pin)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0, r.text
    # ...and it is the same lock as sign-in, so signing in with the right PIN from here is refused as well.
    assert pin_login(client, staff, pin).status_code == 429

    old = settings.login_lockout_minutes
    settings.login_lockout_minutes = 0  # simulate the window having passed
    try:
        ok(attempt(pin))                      # the right PIN works again and changes it
    finally:
        settings.login_lockout_minutes = old
    ok(pin_login(client, staff, "913572"))   # the new PIN signs in; the failure count was cleared


def test_wrong_pins_from_sign_in_and_change_pin_add_up(client, owner_token):
    limiter.reset()
    staff, token, pin = _fresh_staff_session(client, owner_token, "addup")
    bad = "000000" if pin != "000000" else "111111"
    for _ in range(settings.login_max_failures - 1):
        assert pin_login(client, staff, bad).status_code == 401
    assert client.post("/api/v1/auth/change-pin", headers=H(token),
                       json={"current_pin": bad, "new_pin": "913572"}).status_code == 401  # the 5th wrong guess
    assert client.post("/api/v1/auth/change-pin", headers=H(token),
                       json={"current_pin": pin, "new_pin": "913572"}).status_code == 429


def test_change_password_cannot_be_used_to_guess_the_password(client, owner_token):
    limiter.reset()
    attempt = lambda current: client.post("/api/v1/profile/change-password", headers=H(owner_token),
                                          json={"current_password": current, "new_password": "ownerpass1-new"})
    for _ in range(settings.login_max_failures):
        assert attempt("not-the-password").status_code == 401
    r = attempt(OWNER["password"])
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0, r.text
    assert client.post("/api/v1/auth/login", json=OWNER).status_code == 429  # same lock as sign-in

    old = settings.login_lockout_minutes
    settings.login_lockout_minutes = 0  # window passed: clear the failures so later tests can sign in
    try:
        ok(client.post("/api/v1/auth/login", json=OWNER))
    finally:
        settings.login_lockout_minutes = old
    ok(client.post("/api/v1/auth/login", json=OWNER))


def test_a_successful_pin_change_resets_the_counter(client, owner_token):
    limiter.reset()
    staff, token, pin = _fresh_staff_session(client, owner_token, "reset")
    bad = "000000" if pin != "000000" else "111111"
    for _ in range(settings.login_max_failures - 1):
        assert client.post("/api/v1/auth/change-pin", headers=H(token),
                           json={"current_pin": bad, "new_pin": "913572"}).status_code == 401
    token = ok(client.post("/api/v1/auth/change-pin", headers=H(token), json={"current_pin": pin, "new_pin": "913572"}))["token"]
    for _ in range(settings.login_max_failures - 1):  # a fresh allowance, not the leftover one guess
        assert client.post("/api/v1/auth/change-pin", headers=H(token),
                           json={"current_pin": bad, "new_pin": "246810"}).status_code == 401


def test_staff_can_sign_in_with_mobile_number_and_pin(client, owner_token):
    staff = new_staff(client, owner_token, "phone-login")
    ok_resp = client.post("/api/v1/auth/login-pin", json={"phone": staff["phone"], "pin": staff["pin"]})
    assert ok_resp.status_code == 200 and ok_resp.json()["user"]["id"] == staff["id"]
    # a wrong PIN or an unknown number gets the same answer, and the account ID form still works
    assert client.post("/api/v1/auth/login-pin", json={"phone": staff["phone"], "pin": wrong_pin(staff)}).status_code == 401
    assert client.post("/api/v1/auth/login-pin", json={"phone": "9000099999", "pin": staff["pin"]}).status_code == 401
    assert client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": staff["pin"]}).status_code == 200
    # one of the two identifiers is required
    assert client.post("/api/v1/auth/login-pin", json={"pin": staff["pin"]}).status_code == 422


def test_mobile_number_typing_variants_all_match(client, owner_token):
    staff = new_staff(client, owner_token, "variants")
    digits = staff["phone"]
    assert len(digits) == 10  # stored as plain 10 digits
    for typed in (digits, f"+91{digits}", f"91 {digits[:5]} {digits[5:]}", f"0{digits}", f"{digits[:5]}-{digits[5:]}", f" {digits} "):
        r = client.post("/api/v1/auth/login-pin", json={"phone": typed, "pin": staff["pin"]})
        assert r.status_code == 200, typed
