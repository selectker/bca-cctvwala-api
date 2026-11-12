"""Audit log + My profile, end to end against a real PostgreSQL (same setup as test_api.py).

Runs on an empty database or after test_api.py (it bootstraps the admin / owner only when they don't
exist yet, using the same credentials test_api.py does) and puts the owner's password back afterwards.
"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import first_login

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
WIDE = {"from": "2000-01-01", "to": "2100-01-01"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]  # only the two bootstrap endpoints look at it
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


def login(client, creds):
    return ok(client.post("/api/v1/auth/login", json=creds))["token"]


def logs(client, token, **params):
    r = client.get("/api/v1/audit-logs", headers=H(token), params={**WIDE, **params})
    assert r.status_code == 200, r.text
    return r.json(), int(r.headers["X-Total-Count"])


def test_audit_log_and_profile(client):
    # ---- accounts (bootstrap only if this runs on an empty database) ----
    state = ok(client.get("/api/v1/auth/bootstrap-status"))
    if state["needs_admin_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}), 201)
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_owner_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}), 201)
    ot = login(client, OWNER)
    suffix = str(int(time.time() * 1000))[-8:]
    staff_phone = f"97{suffix}"
    staff = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Audit Staff", "phone": staff_phone, "role": "STAFF"}), 201)
    st = first_login(client, staff)

    # ---- writes are recorded: who, what, summary, request body ----
    cust = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "Audit Cust", "phone": "9333333333"}), 201)
    ok(client.put(f"/api/v1/customers/{cust['id']}", headers=H(ot), json={"name": "Audit Cust 2", "phone": "9333333333"}))
    rows, total = logs(client, ot, entity_type="customer", entity_id=cust["id"])  # filters are case-insensitive
    assert total == 2 and [r["action"] for r in rows] == ["UPDATE", "CREATE"]
    assert rows[0]["summary"] == "Updated customer Audit Cust 2" and rows[1]["summary"] == "Created customer Audit Cust"
    assert rows[0]["user_name"] == "Owner" and rows[0]["user_role"] == "OWNER" and "details" not in rows[0]
    detail = ok(client.get(f"/api/v1/audit-logs/{rows[0]['id']}", headers=H(ot)))
    assert detail["details"]["request"]["name"] == "Audit Cust 2" and detail["details"]["endpoint"].startswith("PUT ")

    # deletes keep the record's name (the response itself is empty)
    assert client.delete(f"/api/v1/customers/{cust['id']}", headers=H(ot)).status_code == 204
    rows, _ = logs(client, ot, entity_id=cust["id"], action="DELETE")
    assert rows[0]["summary"] == "Deleted customer Audit Cust 2"

    # failed requests are not recorded as actions
    before = logs(client, ot)[1]
    assert client.put("/api/v1/store-profile", headers=H(st), json={"store_name": "Z"}).status_code == 403
    assert logs(client, ot)[1] == before

    # secrets never reach the log
    rows, _ = logs(client, ot, entity_type="USER", entity_id=staff["id"], action="CREATE")
    created = ok(client.get(f"/api/v1/audit-logs/{rows[0]['id']}", headers=H(ot)))
    assert rows[0]["summary"] == "Created user Audit Staff"
    assert "pin" not in created["details"].get("request", {}) and "pin" not in json.dumps(created["details"]).lower().replace("phone", "")

    # ---- filters, paging, meta ----
    rows, total = logs(client, ot, user_id=staff["id"])
    assert all(r["user_id"] == staff["id"] for r in rows) and total >= 1  # at least the staff PIN sign-in
    assert logs(client, ot, search="audit cust")[1] >= 3
    assert logs(client, ot, **{"from": "2100-01-01", "to": "2100-01-02"})[1] == 0
    page = client.get("/api/v1/audit-logs", headers=H(ot), params={**WIDE, "limit": 1})
    assert len(page.json()) == 1 and int(page.headers["X-Total-Count"]) > 1
    assert client.get("/api/v1/audit-logs", headers=H(ot), params={"user_id": "nope"}).status_code == 400
    assert client.get("/api/v1/audit-logs", headers=H(ot), params={"from": "01-02-2026"}).status_code == 400
    assert client.get("/api/v1/audit-logs/999999999", headers=H(ot)).status_code == 404
    meta = ok(client.get("/api/v1/audit-logs/meta", headers=H(ot)))
    assert "CANCEL" in meta["actions"] and "INVOICE" in meta["entity_types"]

    # ---- sign-ins, including failed ones ----
    assert client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": "000000"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"phone": "9111100000", "password": "whatever1"}).status_code == 401
    assert logs(client, ot, user_id=staff["id"], action="LOGIN")[1] >= 1
    failed, _ = logs(client, ot, user_id=staff["id"], action="LOGIN_FAILED")
    assert failed and failed[0]["summary"] == "Failed sign-in attempt"
    # a failed sign-in against an unknown phone is counted for the lock-outs (login_failures) but never reaches the
    # append-only audit log, so guessing can't flood it and what was typed is not kept
    every_failed, _ = logs(client, ot, action="LOGIN_FAILED")
    assert every_failed and all(r["user_id"] for r in every_failed)
    failed_row = ok(client.get(f"/api/v1/audit-logs/{failed[0]['id']}", headers=H(ot)))
    assert failed_row["details"]["attempts"] >= 1 and "whatever1" not in json.dumps(failed_row)

    # ---- access control: owner only; the platform admin is kept apart ----
    for url in ("/api/v1/audit-logs", "/api/v1/audit-logs/meta", "/api/v1/audit-logs/1", "/api/v1/admin/audit-logs"):
        assert client.get(url, headers=H(st)).status_code == 403
    assert client.get("/api/v1/audit-logs").status_code == 401
    at = login(client, ADMIN)
    assert client.get("/api/v1/audit-logs", headers=H(at)).status_code == 403
    assert client.get("/api/v1/admin/audit-logs", headers=H(ot)).status_code == 403
    rows, total = logs(client, ot)
    assert total > 0 and all(r["user_role"] != "ADMIN" for r in rows)
    admin_rows = ok(client.get("/api/v1/admin/audit-logs", headers=H(at)))
    assert admin_rows and all(r["user_role"] == "ADMIN" for r in admin_rows) and "LOGIN" in {r["action"] for r in admin_rows}
    admin_first = admin_rows[0]["id"]
    assert client.get(f"/api/v1/audit-logs/{admin_first}", headers=H(ot)).status_code == 404  # not even by id

    # ---- profile: read ----
    me = ok(client.get("/api/v1/profile", headers=H(ot)))
    assert me["name"] == "Owner" and me["phone"] == OWNER["phone"] and me["role"] == "OWNER"
    assert me["credential"] == "PASSWORD" and me["last_login_at"] is not None and "password_hash" not in me
    sme = ok(client.get("/api/v1/profile", headers=H(st)))
    assert sme["credential"] == "PIN" and sme["must_change_pin"] is False and sme["email"] is None
    assert ok(client.get("/api/v1/profile", headers=H(at)))["credential"] == "PASSWORD"
    assert client.get("/api/v1/profile").status_code == 401

    # ---- profile: edit ----
    assert client.put("/api/v1/profile", headers=H(st), json={}).status_code == 400
    assert client.put("/api/v1/profile", headers=H(st), json={"name": "   "}).status_code == 400
    assert client.put("/api/v1/profile", headers=H(st), json={"name": None}).status_code == 400
    assert client.put("/api/v1/profile", headers=H(st), json={"phone": "12"}).status_code == 422
    assert client.put("/api/v1/profile", headers=H(st), json={"email": "not-an-email"}).status_code == 422
    assert client.put("/api/v1/profile", headers=H(st), json={"phone": OWNER["phone"]}).status_code == 409  # someone else's
    edited = ok(client.put("/api/v1/profile", headers=H(st), json={"name": " Audit Staff Renamed ", "email": "Sam@Example.com "}))
    assert edited["name"] == "Audit Staff Renamed" and edited["email"] == "Sam@Example.com" and edited["role"] == "STAFF"
    only_phone = ok(client.put("/api/v1/profile", headers=H(st), json={"phone": f"96{suffix}"}))
    assert only_phone["phone"] == f"96{suffix}" and only_phone["name"] == "Audit Staff Renamed"  # untouched fields stay
    assert ok(client.put("/api/v1/profile", headers=H(st), json={"email": None}))["email"] is None  # email can be cleared
    assert ok(client.put("/api/v1/profile", headers=H(st), json={"name": "Audit Staff Renamed"}))["name"] == "Audit Staff Renamed"  # no-op
    # an edit can't touch role / activity, and the owner sees the new name in the team list
    assert client.put("/api/v1/profile", headers=H(st), json={"role": "OWNER", "is_active": False, "name": "Audit Staff Renamed"}).status_code == 200
    assert ok(client.get("/api/v1/profile", headers=H(st)))["role"] == "STAFF"
    assert "Audit Staff Renamed" in [u["name"] for u in ok(client.get("/api/v1/auth/team", headers=H(ot)))]
    rows, _ = logs(client, ot, user_id=staff["id"], action="UPDATE_PROFILE")
    assert [r["summary"] for r in rows] == ["Updated own profile (email)", "Updated own profile (phone)",
                                            "Updated own profile (name, email)"]  # newest first
    changed = ok(client.get(f"/api/v1/audit-logs/{rows[-1]['id']}", headers=H(ot)))["details"]["changed"]
    assert changed["name"] == {"from": "Audit Staff", "to": "Audit Staff Renamed"} and changed["email"]["from"] is None
    assert len(rows) == 3  # the no-op and the ignored-fields edit wrote nothing

    # ---- profile: change password (owner / admin only) ----
    NEW = "brandnewpass1"
    pw = "/api/v1/profile/change-password"
    assert client.post(pw, headers=H(st), json={"current_password": "x", "new_password": NEW}).status_code == 403  # PIN accounts
    assert client.post(pw, headers=H(ot), json={"current_password": "wrongpass1", "new_password": NEW}).status_code == 401
    assert client.post(pw, headers=H(ot), json={"current_password": OWNER["password"], "new_password": "short"}).status_code == 422
    assert client.post(pw, headers=H(ot), json={"current_password": OWNER["password"], "new_password": OWNER["password"]}).status_code == 400
    for weak in ("onlyletters", "12345678", "aaaa1111"):  # needs a letter AND a number, and some variety
        assert client.post(pw, headers=H(ot), json={"current_password": OWNER["password"], "new_password": weak}).status_code == 400
    time.sleep(1.1)
    try:
        res = ok(client.post(pw, headers=H(ot), json={"current_password": OWNER["password"], "new_password": NEW}))
        assert res["token"] and res["user"]["credential"] == "PASSWORD" and "password_hash" not in res["user"]
        assert client.get("/api/v1/profile", headers=H(ot)).status_code == 401           # old token is dead
        assert client.post("/api/v1/auth/login", json=OWNER).status_code == 401           # old password too
        ot2 = ok(client.post("/api/v1/auth/login", json={"phone": OWNER["phone"], "password": NEW}))["token"]
        assert client.get("/api/v1/profile", headers=H(res["token"])).status_code == 200  # token from the change works
        # the log says it happened, never what the passwords were
        rows, _ = logs(client, ot2, action="CHANGE_PASSWORD")
        assert rows and rows[0]["summary"] == "Changed own password"
        text = json.dumps(ok(client.get(f"/api/v1/audit-logs/{rows[0]['id']}", headers=H(ot2))))
        assert NEW not in text and OWNER["password"] not in text
        # ---- my activity ----
        act = client.get("/api/v1/profile/activity", headers=H(ot2), params=WIDE)
        mine = ok(act)
        assert mine and int(act.headers["X-Total-Count"]) >= 2 and "ip_address" not in mine[0] and "user_id" not in mine[0]
        assert {"CHANGE_PASSWORD", "LOGIN"} <= {r["action"] for r in mine}
        # another account's id in the query is ignored: it is always my own history
        sact = ok(client.get("/api/v1/profile/activity", headers=H(st), params={**WIDE, "user_id": me["id"]}))
        assert sact and {r["action"] for r in sact} <= {"LOGIN", "LOGIN_FAILED", "UPDATE_PROFILE", "CHANGE_PIN"}
    finally:
        # leave the owner exactly as found, so other tests (and re-runs) still sign in
        if client.post("/api/v1/auth/login", json=OWNER).status_code == 401:  # the password is still the new one
            cur = ok(client.post("/api/v1/auth/login", json={"phone": OWNER["phone"], "password": NEW}))["token"]
            ok(client.post(pw, headers=H(cur), json={"current_password": NEW, "new_password": OWNER["password"]}))
    assert client.post("/api/v1/auth/login", json=OWNER).status_code == 200
