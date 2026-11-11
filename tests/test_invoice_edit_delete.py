"""v1.33.0: edit a live invoice (PUT /invoices/{id}) and delete a cancelled one (DELETE /invoices/{id}).

Same setup as test_due_dates_and_payments.py: a real PostgreSQL; the admin / owner are bootstrapped when missing.
Every test makes its own customer / product, so the other tests' data doesn't matter.
"""
import os
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
TODAY = date.today()
LINE = {"description": "Installation", "quantity": 1, "unit_price": 1000, "gst_rate": 18}  # -> Rs. 1180.00


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


def customer(client, owner, **fields):
    r = client.post("/api/v1/customers", headers=H(owner),
                    json={"name": f"Edit {time.time_ns()}", "state_code": "29", **fields})
    assert r.status_code == 201, r.text
    return r.json()


def product(client, owner, stock=10, serialized=False):
    r = client.post("/api/v1/products", headers=H(owner), json={
        "name": f"Cam {time.time_ns()}", "sku": f"SKU{time.time_ns()}", "unit_price": 500, "gst_rate": 18,
        "is_serialized": serialized, "opening_stock": 0})
    assert r.status_code == 201, r.text
    p = r.json()
    if stock:
        adj = client.post(f"/api/v1/products/{p['id']}/adjust-stock", headers=H(owner),
                          json={"quantity": stock, "note": "test"})
        assert adj.status_code in (200, 201), adj.text
    return p


def stock(client, owner, product_id):
    rows = client.get("/api/v1/products", headers=H(owner), params={"search": ""}).json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    return float(next(r for r in rows if r["id"] == product_id)["current_stock"])


def invoice(client, owner, cust, items=None, **fields):
    r = client.post("/api/v1/invoices", headers=H(owner),
                    json={"customer_id": cust["id"], "items": items or [LINE], **fields})
    assert r.status_code == 201, r.text
    return r.json()


def edit(client, owner, inv_id, **body):
    return client.put(f"/api/v1/invoices/{inv_id}", headers=H(owner), json=body)


def get(client, owner, inv_id):
    return client.get(f"/api/v1/invoices/{inv_id}", headers=H(owner)).json()


# ------------------------------------------------------------------------------------------------ edit
def test_edit_lines_totals_notes_and_number_stay(client, owner):
    c = customer(client, owner)
    inv = invoice(client, owner, c, notes="first")
    assert float(inv["total_amount"]) == 1180

    r = edit(client, owner, inv["id"], notes="fixed", items=[
        {"description": "Installation", "quantity": 2, "unit_price": 1000, "gst_rate": 18},
        {"description": "Cable", "quantity": 1, "unit_price": 500, "gst_rate": 18, "discount_percent": 10},
    ])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["id"] == inv["id"] and out["invoice_no"] == inv["invoice_no"] and out["customer_id"] == c["id"]
    assert out["notes"] == "fixed" and len(out["items"]) == 2
    # 2000 + (500 - 50) = 2450 taxable, + 18% = 2891
    assert float(out["subtotal"]) == 2450 and float(out["total_amount"]) == 2891
    assert float(out["total_discount"]) == 50
    assert get(client, owner, inv["id"])["total_amount"] == out["total_amount"]


def test_edit_keeps_what_is_not_sent(client, owner):
    c = customer(client, owner)
    inv = invoice(client, owner, c, invoice_discount_percent=10, payment_terms_days=15,
                  billing_address="1 Main Rd", shipping_address="2 Side Rd")
    out = edit(client, owner, inv["id"], items=[LINE]).json()
    assert float(out["invoice_discount_percent"]) == 10  # discount untouched
    assert float(out["total_amount"]) == 1062  # 900 + 18%
    assert out["payment_terms_days"] == 15 and out["due_date"] == inv["due_date"]
    assert out["billing_address"] == "1 Main Rd" and out["shipping_address"] == "2 Side Rd"

    cleared = edit(client, owner, inv["id"], items=[LINE], shipping_address=None, invoice_discount_percent=0).json()
    assert cleared["shipping_address"] is None and float(cleared["total_amount"]) == 1180


def test_edit_date_and_due_date(client, owner):
    c = customer(client, owner)
    inv = invoice(client, owner, c, payment_terms_days=10)
    back = TODAY - timedelta(days=5)
    out = edit(client, owner, inv["id"], items=[LINE], invoice_date=back.isoformat()).json()
    assert out["invoice_date"] == back.isoformat()
    assert out["due_date"] == (back + timedelta(days=10)).isoformat()  # terms follow the moved date

    future = (TODAY + timedelta(days=30)).isoformat()
    assert edit(client, owner, inv["id"], items=[LINE], invoice_date=future).status_code == 400
    assert edit(client, owner, inv["id"], items=[LINE], due_date=(back - timedelta(days=1)).isoformat()).status_code == 400
    assert edit(client, owner, inv["id"], items=[LINE], due_date="2030-01-01", payment_terms_days=5).status_code == 422
    removed = edit(client, owner, inv["id"], items=[LINE], payment_terms_days=None, due_date=None).json()
    assert removed["due_date"] is None and removed["payment_terms_days"] is None


def test_edit_stock_goes_back_and_out_again(client, owner):
    c = customer(client, owner)
    p = product(client, owner, stock=10)
    line = lambda q: {"product_id": p["id"], "quantity": q, "unit_price": 500, "gst_rate": 18}
    inv = invoice(client, owner, c, [line(3)])
    assert stock(client, owner, p["id"]) == 7

    assert edit(client, owner, inv["id"], items=[line(5)]).status_code == 200
    assert stock(client, owner, p["id"]) == 5
    assert edit(client, owner, inv["id"], items=[line(1)]).status_code == 200
    assert stock(client, owner, p["id"]) == 9

    # More than is on the shelf (9 + the 1 this invoice already has = 10) is refused, and nothing changes.
    too_many = edit(client, owner, inv["id"], items=[line(11)])
    assert too_many.status_code == 409 and "Not enough stock" in too_many.json()["error"]
    assert stock(client, owner, p["id"]) == 9 and float(get(client, owner, inv["id"])["items"][0]["quantity"]) == 1
    # ...unless confirmed: stock goes negative with a warning.
    forced = edit(client, owner, inv["id"], items=[line(11)], allow_negative_stock=True)
    assert forced.status_code == 200 and forced.json()["warnings"]
    assert stock(client, owner, p["id"]) == -1

    # Cancelling an edited invoice puts back exactly what is still out -- not the old lines as well.
    assert client.post(f"/api/v1/invoices/{inv['id']}/cancel", headers=H(owner), json={"reason": "test"}).status_code == 200
    assert stock(client, owner, p["id"]) == 10


def test_edit_with_payments(client, owner):
    c = customer(client, owner)
    inv = invoice(client, owner, c)
    assert client.post(f"/api/v1/invoices/{inv['id']}/payments", headers=H(owner), json={"amount": 1000}).status_code == 200

    bigger = edit(client, owner, inv["id"], items=[{**LINE, "unit_price": 2000}]).json()
    assert float(bigger["paid_amount"]) == 1000 and bigger["payment_status"] == "PARTIAL"

    smaller = edit(client, owner, inv["id"], items=[{**LINE, "unit_price": 500}])  # Rs. 590 < 1000 received
    assert smaller.status_code == 409 and "already been received" in smaller.json()["error"]
    assert float(get(client, owner, inv["id"])["total_amount"]) == 2360  # unchanged

    paid_up = edit(client, owner, inv["id"], items=[{**LINE, "unit_price": 847.46}])  # 847.46 + 18% = 1000.00
    assert paid_up.status_code == 200, paid_up.text
    assert float(paid_up.json()["total_amount"]) == 1000 and paid_up.json()["payment_status"] == "PAID"


def test_edit_credit_limit_only_checked_when_the_invoice_grows(client, owner):
    c = customer(client, owner, credit_limit=2000)
    inv = invoice(client, owner, c)  # 1180 owed
    over = edit(client, owner, inv["id"], items=[{**LINE, "unit_price": 2000}])  # 2360 > 2000
    assert over.status_code == 409 and "credit limit" in over.json()["error"]
    ok = edit(client, owner, inv["id"], items=[{**LINE, "unit_price": 2000}], allow_over_credit_limit=True)
    assert ok.status_code == 200
    # Already over the limit: a smaller edit / a notes-only edit still goes through.
    assert edit(client, owner, inv["id"], items=[{**LINE, "unit_price": 1900}], notes="smaller").status_code == 200


def test_edit_serial_numbers(client, owner):
    c = customer(client, owner)
    p = product(client, owner, stock=0, serialized=True)
    sup = client.post("/api/v1/suppliers", headers=H(owner), json={"name": f"Sup {time.time_ns()}"}).json()
    barcodes = [f"BC{time.time_ns()}{i}" for i in range(3)]
    purchase = client.post("/api/v1/purchases", headers=H(owner), json={
        "supplier_id": sup["id"], "items": [{"product_id": p["id"], "quantity": 3, "unit_price": 300, "gst_rate": 18,
                                             "barcodes": barcodes, "supplier_warranty_months": 12}]})
    assert purchase.status_code == 201, purchase.text

    def line(codes, months=12):
        return {"product_id": p["id"], "quantity": len(codes), "unit_price": 500, "gst_rate": 18,
                "barcodes": codes, "customer_warranty_months": months}

    inv = invoice(client, owner, c, [line(barcodes[:2])])
    assert get(client, owner, inv["id"])["items"][0]["customer_warranty_months"] == 12

    # Swap one unit for the third and change the warranty length.
    out = edit(client, owner, inv["id"], items=[line([barcodes[0], barcodes[2]], months=24)])
    assert out.status_code == 200, out.text
    item = out.json()["items"][0]
    assert sorted(item["barcodes"]) == sorted([barcodes[0], barcodes[2]]) and item["customer_warranty_months"] == 24
    assert stock(client, owner, p["id"]) == 1  # barcodes[1] is back in stock

    # barcodes[1] can be sold on another invoice now, but not twice.
    other = invoice(client, owner, customer(client, owner), [line([barcodes[1]])])
    assert other["id"] != inv["id"]
    clash = edit(client, owner, inv["id"], items=[line([barcodes[0], barcodes[1]])])
    assert clash.status_code == 409
    assert sorted(get(client, owner, inv["id"])["items"][0]["barcodes"]) == sorted([barcodes[0], barcodes[2]])  # rolled back


def test_cannot_edit_cancelled_or_unknown(client, owner):
    inv = invoice(client, owner, customer(client, owner))
    assert client.post(f"/api/v1/invoices/{inv['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 200
    assert edit(client, owner, inv["id"], items=[LINE]).status_code == 409
    assert edit(client, owner, "00000000-0000-0000-0000-000000000000", items=[LINE]).status_code == 404
    live = invoice(client, owner, customer(client, owner))
    assert edit(client, owner, live["id"], items=[]).status_code == 422
    assert edit(client, owner, live["id"]).status_code == 422


# ---------------------------------------------------------------------------------------------- delete
def test_delete_only_a_cancelled_invoice(client, owner):
    c = customer(client, owner)
    p = product(client, owner, stock=5)
    inv = invoice(client, owner, c, [{"product_id": p["id"], "quantity": 2, "unit_price": 500, "gst_rate": 18}])
    client.post(f"/api/v1/invoices/{inv['id']}/payments", headers=H(owner), json={"amount": 500})
    url = f"/api/v1/invoices/{inv['id']}"

    live = client.delete(url, headers=H(owner))
    assert live.status_code == 409 and "cancel" in live.json()["error"].lower()
    assert client.get(url, headers=H(owner)).status_code == 200

    cancel = client.post(f"{url}/cancel", headers=H(owner), json={"reason": "wrong", "refund_payments": True})
    assert cancel.status_code == 200, cancel.text
    assert client.delete(url, headers=H(owner)).status_code == 204
    assert client.get(url, headers=H(owner)).status_code == 404
    assert client.delete(url, headers=H(owner)).status_code == 404
    assert stock(client, owner, p["id"]) == 5  # the stock the cancel put back stays put

    listed = client.get("/api/v1/invoices", headers=H(owner), params={"customer_id": c["id"]}).json()
    assert inv["id"] not in [row["id"] for row in listed]

    audit = client.get("/api/v1/audit-logs", headers=H(owner), params={"entity_type": "INVOICE", "action": "DELETE"}).json()
    rows = audit["items"] if isinstance(audit, dict) else audit
    assert any(inv["invoice_no"] in (row.get("summary") or "") for row in rows)


def test_delete_frees_nothing_else(client, owner):
    """A cancelled invoice that came from a quotation: deleting it leaves the quotation open to convert again."""
    c = customer(client, owner)
    q = client.post("/api/v1/quotations", headers=H(owner), json={"customer_id": c["id"], "items": [LINE]}).json()
    inv = client.post(f"/api/v1/invoices/from-quotation/{q['id']}", headers=H(owner)).json()
    assert client.post(f"/api/v1/invoices/{inv['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 200
    assert client.delete(f"/api/v1/invoices/{inv['id']}", headers=H(owner)).status_code == 204
    again = client.post(f"/api/v1/invoices/from-quotation/{q['id']}", headers=H(owner))
    assert again.status_code == 201, again.text
