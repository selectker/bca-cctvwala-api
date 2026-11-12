"""v1.37.0: the built-in walk-in customer and the optional buyer name / phone on its invoices.

Same setup as test_due_dates_and_payments.py: a real PostgreSQL; the admin / owner are bootstrapped when missing.
"""
import os
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
LINE = {"description": "Installation", "quantity": 1, "unit_price": 1000, "gst_rate": 18}


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


@pytest.fixture(scope="module")
def walk_in(client, owner):
    r = client.get("/api/v1/customers/walk-in", headers=H(owner))
    assert r.status_code == 200, r.text
    return r.json()


def test_walk_in_is_made_once_and_is_not_in_the_customer_list(client, owner, walk_in):
    assert walk_in["is_walk_in"] is True and walk_in["name"] == "Walk-in Customer" and walk_in["customer_type"] == "B2C"
    again = client.get("/api/v1/customers/walk-in", headers=H(owner)).json()
    assert again["id"] == walk_in["id"]
    listed = client.get("/api/v1/customers", headers=H(owner), params={"search": "Walk-in", "limit": 100}).json()
    assert all(c["id"] != walk_in["id"] for c in listed)


def test_walk_in_cannot_be_edited_or_deleted(client, owner, walk_in):
    body = {"name": "Renamed"}
    assert client.put(f"/api/v1/customers/{walk_in['id']}", headers=H(owner), json=body).status_code == 409
    assert client.delete(f"/api/v1/customers/{walk_in['id']}", headers=H(owner)).status_code == 409
    assert client.get("/api/v1/customers/walk-in", headers=H(owner)).json()["name"] == "Walk-in Customer"


def test_walk_in_invoice_with_and_without_buyer_details(client, owner, walk_in):
    plain = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": walk_in["id"], "items": [LINE]})
    assert plain.status_code == 201, plain.text
    assert plain.json()["buyer_name"] is None and plain.json()["customer_id"] == walk_in["id"]

    named = client.post("/api/v1/invoices", headers=H(owner), json={
        "customer_id": walk_in["id"], "items": [LINE], "buyer_name": "  Rahul Sharma ", "buyer_phone": "9876543210"})
    assert named.status_code == 201, named.text
    inv = named.json()
    assert inv["buyer_name"] == "Rahul Sharma" and inv["buyer_phone"] == "9876543210"

    detail = client.get(f"/api/v1/invoices/{inv['id']}", headers=H(owner)).json()
    assert detail["customer_is_walk_in"] is True and detail["customer_name"] == "Walk-in Customer"
    # findable by the buyer's name or number from the invoice list
    found = client.get("/api/v1/invoices", headers=H(owner), params={"search": "Rahul Sharma"}).json()
    assert any(i["id"] == inv["id"] for i in found)
    pdf = client.get(f"/api/v1/invoices/{inv['id']}/pdf", headers=H(owner))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_buyer_details_are_refused_for_a_normal_customer(client, owner):
    cust = client.post("/api/v1/customers", headers=H(owner), json={"name": f"Reg {time.time_ns()}", "state_code": "29"}).json()
    r = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": cust["id"], "items": [LINE], "buyer_name": "X"})
    assert r.status_code == 422
    ok = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": cust["id"], "items": [LINE]}).json()
    r = client.put(f"/api/v1/invoices/{ok['id']}", headers=H(owner), json={"items": [LINE], "buyer_name": "X"})
    assert r.status_code == 422


def test_buyer_details_can_be_edited_and_cleared_on_a_walk_in_invoice(client, owner, walk_in):
    inv = client.post("/api/v1/invoices", headers=H(owner), json={
        "customer_id": walk_in["id"], "items": [LINE], "buyer_name": "Asha"}).json()
    r = client.put(f"/api/v1/invoices/{inv['id']}", headers=H(owner), json={"items": [LINE], "buyer_phone": "9123456780"})
    assert r.status_code == 200, r.text
    assert r.json()["buyer_name"] == "Asha" and r.json()["buyer_phone"] == "9123456780"  # name untouched
    r = client.put(f"/api/v1/invoices/{inv['id']}", headers=H(owner), json={"items": [LINE], "buyer_name": None})
    assert r.json()["buyer_name"] is None and r.json()["buyer_phone"] == "9123456780"


def test_walk_in_tax_is_cgst_sgst(client, owner, walk_in):
    inv = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": walk_in["id"], "items": [LINE]}).json()
    assert inv["is_interstate"] is False and float(inv["total_igst"]) == 0
