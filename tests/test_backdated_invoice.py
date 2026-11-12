"""Back-dated invoices: a new invoice may carry any date from 1 April of the current financial year up to today.

Same setup as test_due_dates_and_payments.py: a real PostgreSQL; the admin / owner are bootstrapped when missing.
The pure date rules are also checked without a database (first block).
"""
import os
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.billing import ConvertQuotationRequest, CreateInvoiceRequest
from app.utils.dates import financial_year_end, financial_year_start, today_local

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
LINE = {"description": "Installation", "quantity": 1, "unit_price": 1000, "gst_rate": 18}  # -> Rs. 1180.00

FY_START = financial_year_start(today_local())


# ---- no database needed --------------------------------------------------------------------------------------

def test_financial_year_boundaries():
    assert financial_year_start(date(2026, 4, 1)) == date(2026, 4, 1)
    assert financial_year_start(date(2027, 3, 31)) == date(2026, 4, 1)
    assert financial_year_start(date(2026, 3, 31)) == date(2025, 4, 1)
    assert financial_year_end(date(2026, 11, 12)) == date(2027, 3, 31)
    assert financial_year_end(date(2027, 2, 1)) == date(2027, 3, 31)


def _request(**fields):
    return CreateInvoiceRequest(customer_id="c1", items=[LINE], **fields)


def test_schema_accepts_today_fy_start_and_nothing():
    assert _request().invoice_date is None
    assert _request(invoice_date=today_local()).invoice_date == today_local()
    assert _request(invoice_date=FY_START).invoice_date == FY_START


def test_schema_refuses_future_and_previous_financial_year():
    with pytest.raises(ValidationError, match="future"):
        _request(invoice_date=today_local() + timedelta(days=1))
    with pytest.raises(ValidationError, match="current financial year"):
        _request(invoice_date=FY_START - timedelta(days=1))
    with pytest.raises(ValidationError, match="current financial year"):
        ConvertQuotationRequest(invoice_date=FY_START - timedelta(days=1))


def test_due_date_is_checked_against_the_invoice_date_when_back_dated():
    d = today_local() - timedelta(days=3)
    if d < FY_START:
        pytest.skip("not enough days into the financial year")
    assert _request(invoice_date=d, due_date=d + timedelta(days=1)).due_date == d + timedelta(days=1)  # already past: fine
    with pytest.raises(ValidationError, match="before the invoice date"):
        _request(invoice_date=d, due_date=d - timedelta(days=1))
    with pytest.raises(ValidationError, match="in the past"):  # no invoice date: unchanged rule
        _request(due_date=today_local() - timedelta(days=1))


# ---- end to end ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def owner(client):
    if client.get("/api/v1/auth/bootstrap-status").json()["needs_admin_bootstrap"]:
        assert client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}).status_code == 201
    if client.get("/api/v1/auth/bootstrap-status").json()["needs_owner_bootstrap"]:
        assert client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}).status_code == 201
    return client.post("/api/v1/auth/login", json=OWNER).json()["token"]


def _customer(client, owner, **fields):
    r = client.post("/api/v1/customers", headers=H(owner),
                    json={"name": f"Backdate {time.time_ns()}", "state_code": "29", **fields})
    assert r.status_code == 201, r.text
    return r.json()


def _invoice(client, owner, cust, **fields):
    return client.post("/api/v1/invoices", headers=H(owner),
                       json={"customer_id": cust["id"], "items": [LINE], **fields})


def test_invoice_without_a_date_is_dated_today(client, owner):
    r = _invoice(client, owner, _customer(client, owner))
    assert r.status_code == 201, r.text
    assert r.json()["invoice_date"] == today_local().isoformat()


def test_back_dated_invoice_keeps_its_date_and_current_fy_number(client, owner):
    d = FY_START
    r = _invoice(client, owner, _customer(client, owner), invoice_date=d.isoformat())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invoice_date"] == d.isoformat()
    assert f"/{FY_START.year}-{str(FY_START.year + 1)[2:]}/" in body["invoice_no"]
    # It is found by a date filter on its own date, not on today's.
    listed = client.get(f"/api/v1/invoices?from_date={d}&to_date={d}", headers=H(owner))
    assert listed.status_code == 200, listed.text


def test_payment_terms_count_from_the_invoice_date(client, owner):
    d = FY_START
    r = _invoice(client, owner, _customer(client, owner), invoice_date=d.isoformat(), payment_terms_days=15)
    assert r.status_code == 201, r.text
    assert r.json()["due_date"] == (d + timedelta(days=15)).isoformat()


def test_future_and_previous_year_dates_are_refused(client, owner):
    cust = _customer(client, owner)
    future = _invoice(client, owner, cust, invoice_date=(today_local() + timedelta(days=1)).isoformat())
    assert future.status_code == 422, future.text
    old = _invoice(client, owner, cust, invoice_date=(FY_START - timedelta(days=1)).isoformat())
    assert old.status_code == 422, old.text
    assert "financial year" in old.text
