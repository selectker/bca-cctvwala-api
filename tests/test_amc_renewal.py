"""AMC edit, renewal and contract PDF (v1.26.0): helpers and PDF without a database, then the endpoints end to end."""
import os
import time
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers.amc import MAX_VISITS, schedule_dates, term_months
from app.utils.pdf import build_amc_pdf
from tests.helpers import first_login

OWNER = {"phone": "9000000001", "password": "ownerpass1"}
ADMIN = {"phone": "9000000000", "password": "adminpass1"}
STORE = {"store_name": "BCA CCTV", "address": "MG Road", "phone": "99999", "gstin": "29ABCDE1234F1Z5"}
CONTRACT = {
    "contract_no": "AMC/2026-27/0001", "status": "ACTIVE", "customer_name": "Asha", "customer_phone": "88888",
    "customer_address": "12 MG Road\nBengaluru", "customer_gstin": "29AAAAA0000A1Z5", "site_address": "Shop 4",
    "start_date": date(2026, 10, 21), "end_date": date(2027, 10, 20), "visit_frequency": "QUARTERLY", "amount": 12000,
    "gst_rate": 18, "coverage_notes": "8 cameras, DVR, cabling\n" * 30, "renewed_from_no": "AMC/2025-26/0004",
}
VISITS = [{"scheduled_date": date(2026, 10, 21) + timedelta(days=90 * i), "status": "SCHEDULED", "completed_date": None} for i in range(60)]


# ----------------------------------------------------------------------------- helpers and PDF (no database)
def test_visit_plan_follows_the_frequency():
    assert len(schedule_dates(date(2026, 1, 1), date(2026, 12, 31), "QUARTERLY")) == 4
    assert len(schedule_dates(date(2026, 1, 1), date(2026, 12, 31), "MONTHLY")) == 12
    assert schedule_dates(date(2026, 1, 1), date(2027, 1, 1), "YEARLY") == [date(2026, 1, 1), date(2027, 1, 1)]
    assert schedule_dates(date(2026, 5, 1), date(2026, 1, 1), "MONTHLY") == []


def test_a_mistyped_end_date_cannot_create_thousands_of_visits():
    with pytest.raises(HTTPException) as err:
        schedule_dates(date(2026, 1, 1), date(2200, 1, 1), "MONTHLY")
    assert err.value.status_code == 400 and str(MAX_VISITS) in err.value.detail


@pytest.mark.parametrize("start,end,months", [
    (date(2026, 1, 1), date(2026, 12, 31), 12),   # end written as the last day of the term
    (date(2026, 1, 1), date(2027, 1, 1), 12),     # end written as the same day a year later
    (date(2026, 1, 1), date(2026, 6, 30), 6),
    (date(2026, 1, 1), date(2026, 3, 17), 12),    # not a whole number of months: a year
])
def test_term_length_in_months(start, end, months):
    assert term_months(start, end) == months


def test_contract_pdf_renders_full_sparse_cancelled_and_long():
    assert build_amc_pdf(STORE, CONTRACT, VISITS[:4]).startswith(b"%PDF")
    assert build_amc_pdf(STORE, CONTRACT, VISITS).startswith(b"%PDF")  # many visits and long notes run onto more pages
    assert build_amc_pdf({}, {"customer_name": "A", "start_date": date(2026, 1, 1), "end_date": date(2027, 1, 1)}, []).startswith(b"%PDF")
    assert build_amc_pdf(STORE, {**CONTRACT, "status": "CANCELLED"}, VISITS[:4]).startswith(b"%PDF")
    assert build_amc_pdf(STORE, CONTRACT, VISITS[:4], b"garbage", b"garbage").startswith(b"%PDF")  # a bad image never stops it


# ----------------------------------------------------------------------------- end to end (needs PostgreSQL, see README 2m)
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


def test_edit_and_renew_end_to_end(client):
    state = ok(client.get("/api/v1/auth/bootstrap-status"))
    if state["needs_admin_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}), 201)
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_owner_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}), 201)
    ot = ok(client.post("/api/v1/auth/login", json=OWNER))["token"]
    suffix = str(int(time.time() * 1000))[-8:]
    staff = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Amc Staff", "phone": f"95{suffix}", "role": "STAFF"}), 201)
    st = first_login(client, staff)
    cust = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "AMC Renewal Cust", "phone": f"94{suffix}", "address": "5 Lake Rd"}), 201)

    amc = ok(client.post("/api/v1/amc", headers=H(ot), json={
        "customer_id": cust["id"], "start_date": "2026-01-01", "end_date": "2026-12-31", "amount": 6000,
        "visit_frequency": "QUARTERLY", "coverage_notes": "4 cameras"}), 201)
    url = f"/api/v1/amc/{amc['id']}"
    visits = ok(client.get(url, headers=H(ot)))["visits"]
    assert len(visits) == 4
    ok(client.put(f"/api/v1/amc/visits/{visits[0]['id']}", headers=H(ot), json={"status": "COMPLETED", "remarks": "done"}))

    # ---- edit: only the fields sent change; money changes leave the visit plan alone
    edited = ok(client.put(url, headers=H(ot), json={"amount": 7000, "coverage_notes": "8 cameras"}))
    assert float(edited["amount"]) == 7000 and edited["coverage_notes"] == "8 cameras" and edited["visit_frequency"] == "QUARTERLY"
    assert edited["customer_name"] == "AMC Renewal Cust" and len(edited["visits"]) == 4
    assert ok(client.put(url, headers=H(ot), json={"site_address": None}))["site_address"] is None

    # ---- edit: the visit plan follows the frequency, and never deletes a visit somebody has worked on
    monthly = ok(client.put(url, headers=H(ot), json={"visit_frequency": "MONTHLY"}))
    assert len(monthly["visits"]) == 12 and any(v["status"] == "COMPLETED" for v in monthly["visits"])
    yearly = ok(client.put(url, headers=H(ot), json={"visit_frequency": "YEARLY"}))
    assert [v["status"] for v in yearly["visits"]] == ["COMPLETED"]  # the untouched planned ones are gone, the done one stays

    # ---- edit: validation and access
    assert client.put(url, headers=H(ot), json={}).status_code == 400
    assert client.put(url, headers=H(ot), json={"end_date": "2025-01-01"}).status_code == 400
    assert client.put(url, headers=H(ot), json={"amount": None}).status_code == 400
    assert client.put(url, headers=H(ot), json={"visit_frequency": "WEEKLY"}).status_code == 422
    assert client.put("/api/v1/amc/not-a-uuid", headers=H(ot), json={"amount": 1}).status_code == 404
    assert client.put(url, headers=H(st), json={"amount": 1}).status_code == 403
    assert client.put(url, json={"amount": 1}).status_code == 401
    ok(client.put(f"{url}/status", headers=H(ot), json={"status": "CANCELLED"}))
    assert client.put(url, headers=H(ot), json={"amount": 1}).status_code == 409   # only an ACTIVE contract is editable
    assert client.post(f"{url}/renew", headers=H(ot), json={}).status_code == 409  # nor is a cancelled one renewable
    ok(client.put(f"{url}/status", headers=H(ot), json={"status": "ACTIVE"}))

    # ---- renew: a new contract that carries everything over and points back
    renewed = ok(client.post(f"{url}/renew", headers=H(ot), json={}), 201)
    assert renewed["id"] != amc["id"] and renewed["contract_no"] != amc["contract_no"]
    assert (renewed["start_date"], renewed["end_date"]) == ("2027-01-01", "2027-12-31")
    assert float(renewed["amount"]) == 7000 and renewed["visit_frequency"] == "YEARLY" and renewed["coverage_notes"] == "8 cameras"
    assert renewed["customer_name"] == "AMC Renewal Cust" and len(renewed["visits"]) == 1
    assert renewed["renewed_from_contract_no"] == amc["contract_no"] and renewed["status"] == "ACTIVE"
    old = ok(client.get(url, headers=H(ot)))
    assert old["renewed_to_id"] == renewed["id"] and old["renewed_to_contract_no"] == renewed["contract_no"]
    assert client.post(f"{url}/renew", headers=H(ot), json={}).status_code == 409   # once only
    assert client.post(f"{url}/renew", headers=H(st), json={}).status_code == 403
    assert client.post("/api/v1/amc/not-a-uuid/renew", headers=H(ot), json={}).status_code == 404

    # ---- renew with changes
    second = ok(client.post(f"/api/v1/amc/{renewed['id']}/renew", headers=H(ot), json={
        "amount": 9000, "visit_frequency": "HALF_YEARLY", "coverage_notes": None, "end_date": "2028-12-31"}), 201)
    assert second["start_date"] == "2028-01-01" and second["end_date"] == "2028-12-31" and float(second["amount"]) == 9000
    assert second["coverage_notes"] is None and len(second["visits"]) == 2
    assert client.post(f"/api/v1/amc/{second['id']}/renew", headers=H(ot), json={"end_date": "2020-01-01"}).status_code == 400

    # ---- a renewed contract stops being "expiring"; one that isn't renewed still is
    today = date.today()
    due = ok(client.post("/api/v1/amc", headers=H(ot), json={
        "customer_id": cust["id"], "start_date": str(today - timedelta(days=355)), "end_date": str(today + timedelta(days=10)), "amount": 100}), 201)
    ids = lambda: {c["id"] for c in ok(client.get("/api/v1/amc/expiring", headers=H(ot), params={"days": 30, "limit": 500}))}  # noqa: E731
    assert due["id"] in ids()
    ok(client.post(f"/api/v1/amc/{due['id']}/renew", headers=H(ot), json={}), 201)
    assert due["id"] not in ids()
    listed = ok(client.get("/api/v1/amc", headers=H(ot), params={"expiring_within_days": 30, "limit": 500}))
    assert due["id"] not in {c["id"] for c in listed}

    # ---- contract PDF
    pdf = client.get(f"{url}/pdf", headers=H(ot))
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert client.get(f"/api/v1/amc/{renewed['id']}/pdf", headers=H(ot)).content.startswith(b"%PDF")
    assert client.get("/api/v1/amc/00000000-0000-4000-8000-000000000000/pdf", headers=H(ot)).status_code == 404
    assert client.get("/api/v1/amc/not-a-uuid/pdf", headers=H(ot)).status_code == 404
    assert client.get(f"{url}/pdf", headers=H(st)).status_code == 403
    assert client.get(f"{url}/pdf").status_code == 401
