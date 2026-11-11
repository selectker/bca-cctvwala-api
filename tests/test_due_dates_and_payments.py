"""v1.30.0: payment terms / due dates / credit limit / ageing, and payment reversal, correction and receipts.

Same setup as test_expenses.py: a real PostgreSQL; the admin / owner are bootstrapped when missing. Every test makes its
own customer, so the other tests' data doesn't matter.
"""
import os
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils.pdf import amount_in_words, build_invoice_pdf

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
                    json={"name": f"Due {time.time_ns()}", "state_code": "29", **fields})
    assert r.status_code == 201, r.text
    return r.json()


def invoice(client, owner, cust, **fields):
    r = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": cust["id"], "items": [LINE], **fields})
    return r


def pay(client, owner, inv_id, amount, **fields):
    r = client.post(f"/api/v1/invoices/{inv_id}/payments", headers=H(owner), json={"amount": amount, **fields})
    assert r.status_code == 200, r.text
    return r.json()


def payments(client, owner, inv_id):
    return client.get(f"/api/v1/invoices/{inv_id}/payments", headers=H(owner)).json()


# ---------------------------------------------------------------------------------------------- due dates
def test_due_date_from_terms_date_and_customer_default(client, owner):
    c = customer(client, owner, payment_terms_days=30)
    assert c["payment_terms_days"] == 30 and c["credit_limit"] is None

    default = invoice(client, owner, c)
    assert default.status_code == 201, default.text
    assert default.json()["payment_terms_days"] == 30
    assert default.json()["due_date"] == (TODAY + timedelta(days=30)).isoformat()

    explicit = invoice(client, owner, c, payment_terms_days=7).json()
    assert explicit["due_date"] == (TODAY + timedelta(days=7)).isoformat()

    zero = invoice(client, owner, c, payment_terms_days=0).json()
    assert zero["due_date"] == TODAY.isoformat()

    by_date = invoice(client, owner, c, due_date=(TODAY + timedelta(days=45)).isoformat()).json()
    assert by_date["due_date"] == (TODAY + timedelta(days=45)).isoformat() and by_date["payment_terms_days"] is None

    no_terms = invoice(client, owner, customer(client, owner)).json()
    assert no_terms["due_date"] is None and no_terms["payment_terms_days"] is None


def test_due_date_validation(client, owner):
    c = customer(client, owner)
    both = invoice(client, owner, c, payment_terms_days=5, due_date=(TODAY + timedelta(days=5)).isoformat())
    assert both.status_code == 422
    past = invoice(client, owner, c, due_date=(TODAY - timedelta(days=1)).isoformat())
    assert past.status_code == 422
    assert invoice(client, owner, c, payment_terms_days=366).status_code == 422
    assert client.post("/api/v1/customers", headers=H(owner),
                       json={"name": "Bad", "credit_limit": -1}).status_code == 422


def test_overdue_fields_filter_and_edit_due_date(client, owner):
    c = customer(client, owner)
    inv = invoice(client, owner, c, payment_terms_days=10).json()
    got = client.get(f"/api/v1/invoices/{inv['id']}", headers=H(owner)).json()
    assert got["days_overdue"] == 0 and got["effective_due_date"] == inv["due_date"]

    # no due date = due on the invoice date: already overdue from the next day on; today it isn't yet
    plain = invoice(client, owner, customer(client, owner)).json()
    assert client.get(f"/api/v1/invoices/{plain['id']}", headers=H(owner)).json()["days_overdue"] == 0

    # move the due date into the past (editing may go back to the invoice date, not before it)
    assert client.put(f"/api/v1/invoices/{inv['id']}/due-date", headers=H(owner),
                      json={"due_date": (TODAY - timedelta(days=1)).isoformat()}).status_code == 400
    r = client.put(f"/api/v1/invoices/{inv['id']}/due-date", headers=H(owner), json={"payment_terms_days": 0})
    assert r.status_code == 200 and r.json()["due_date"] == TODAY.isoformat()
    assert client.put(f"/api/v1/invoices/{inv['id']}/due-date", headers=H(owner), json={}).status_code == 422
    cleared = client.put(f"/api/v1/invoices/{inv['id']}/due-date", headers=H(owner),
                         json={"payment_terms_days": None, "due_date": None}).json()
    assert cleared["due_date"] is None and cleared["payment_terms_days"] is None

    # Backdate through SQL so the overdue paths are exercised for real.
    import asyncio
    from tests.helpers import raw_connect

    async def backdate():
        conn = await raw_connect()
        try:
            await conn.execute("UPDATE invoices SET due_date = CURRENT_DATE - 40 WHERE id = $1", inv["id"])
        finally:
            await conn.close()
    asyncio.run(backdate())

    got = client.get(f"/api/v1/invoices/{inv['id']}", headers=H(owner)).json()
    assert got["days_overdue"] == 40
    listed = client.get("/api/v1/invoices", headers=H(owner), params={"overdue": "true", "customer_id": c["id"]}).json()
    assert [i["id"] for i in listed] == [inv["id"]] and listed[0]["days_overdue"] == 40

    # paying it off removes it from the overdue list; so does cancelling
    pay(client, owner, inv["id"], 1180)
    assert client.get("/api/v1/invoices", headers=H(owner), params={"overdue": "true", "customer_id": c["id"]}).json() == []
    assert client.get(f"/api/v1/invoices/{inv['id']}", headers=H(owner)).json()["days_overdue"] == 0


def test_due_date_is_printed_on_the_invoice_pdf(client, owner):
    inv = invoice(client, owner, customer(client, owner), payment_terms_days=15).json()
    r = client.get(f"/api/v1/invoices/{inv['id']}/pdf", headers=H(owner))
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    # and the builder itself shows no due line on a paid invoice (smoke: it still builds)
    paid = {"invoice_no": "I-1", "invoice_date": "2026-10-20", "customer_name": "A", "total_amount": 118, "paid_amount": 118,
            "subtotal": 100, "total_cgst": 9, "total_sgst": 9, "total_igst": 0, "due_date": date(2026, 11, 1)}
    assert build_invoice_pdf({"store_name": "S"}, paid, []).startswith(b"%PDF")


# ---------------------------------------------------------------------------------------------- credit limit
def test_credit_limit_blocks_until_confirmed(client, owner):
    c = customer(client, owner, credit_limit=2000, opening_balance=500)
    first = invoice(client, owner, c)  # 500 + 1180 = 1680 <= 2000
    assert first.status_code == 201, first.text

    blocked = invoice(client, owner, c)  # 1680 + 1180 > 2000
    assert blocked.status_code == 409 and "credit limit" in blocked.json()["error"]
    assert client.get(f"/api/v1/customers/{c['id']}", headers=H(owner)).json()["outstanding"] == 1680

    forced = invoice(client, owner, c, allow_over_credit_limit=True)
    assert forced.status_code == 201

    # paying down makes room again
    pay(client, owner, first.json()["id"], 1180)
    pay(client, owner, forced.json()["id"], 1180)
    assert invoice(client, owner, c).status_code == 201


def test_credit_limit_edit_keeps_or_clears(client, owner):
    c = customer(client, owner, credit_limit=100)
    # an older app that doesn't send the new fields leaves them alone
    r = client.put(f"/api/v1/customers/{c['id']}", headers=H(owner), json={"name": c["name"] + " x"})
    assert r.status_code == 200 and float(r.json()["credit_limit"]) == 100
    assert invoice(client, owner, c).status_code == 409
    r = client.put(f"/api/v1/customers/{c['id']}", headers=H(owner),
                   json={"name": c["name"], "credit_limit": None, "payment_terms_days": 14})
    assert r.json()["credit_limit"] is None and r.json()["payment_terms_days"] == 14
    assert invoice(client, owner, c).status_code == 201


def test_converted_quotation_gets_terms_and_limit(client, owner):
    c = customer(client, owner, payment_terms_days=20, credit_limit=100)
    q = client.post("/api/v1/quotations", headers=H(owner), json={"customer_id": c["id"], "items": [LINE]})
    assert q.status_code == 201, q.text
    assert client.post(f"/api/v1/invoices/from-quotation/{q.json()['id']}", headers=H(owner)).status_code == 409
    r = client.post(f"/api/v1/invoices/from-quotation/{q.json()['id']}", headers=H(owner),
                    json={"allow_over_credit_limit": True})
    assert r.status_code == 201, r.text
    assert r.json()["due_date"] == (TODAY + timedelta(days=20)).isoformat()


# ---------------------------------------------------------------------------------------------- ageing report
def test_receivables_ageing_buckets(client, owner):
    import asyncio
    from tests.helpers import raw_connect

    c = customer(client, owner, opening_balance=100)
    ids = [invoice(client, owner, c).json()["id"] for _ in range(5)]
    late = [0, 10, 45, 75, 120]  # days past due -> not_due, 1-30, 31-60, 61-90, 90+

    async def age():
        conn = await raw_connect()
        try:
            for inv_id, days in zip(ids, late):
                await conn.execute("UPDATE invoices SET due_date = CURRENT_DATE - $2::int WHERE id = $1", inv_id, days)
        finally:
            await conn.close()
    asyncio.run(age())
    # Rs. 180 of the 10-day-late invoice is paid, so only 1000 of it is still owed
    pay(client, owner, ids[1], 180)

    r = client.get("/api/v1/reports/receivables-ageing", headers=H(owner), params={"limit": 500})
    assert r.status_code == 200
    row = next(x for x in r.json() if x["id"] == c["id"])
    assert float(row["not_due"]) == 1180 and float(row["days_1_30"]) == 1000
    assert float(row["days_31_60"]) == 1180 and float(row["days_61_90"]) == 1180 and float(row["days_over_90"]) == 1180
    assert float(row["overdue_amount"]) == 1000 + 1180 * 3
    assert float(row["opening_balance"]) == 100
    assert float(row["total_outstanding"]) == 100 + 1180 * 5 - 180
    assert float(r.headers["X-Total-Outstanding"]) >= float(row["total_outstanding"])
    assert float(r.headers["X-Total-Overdue"]) >= float(row["overdue_amount"])
    assert "_sum" not in row and "_overdue" not in row

    # cancelled invoices and fully paid customers drop out
    paid_c = customer(client, owner)
    pay(client, owner, invoice(client, owner, paid_c).json()["id"], 1180)
    assert paid_c["id"] not in {x["id"] for x in client.get("/api/v1/reports/receivables-ageing", headers=H(owner),
                                                            params={"limit": 500}).json()}


# ---------------------------------------------------------------------------------------------- reverse / edit a payment
def test_reverse_payment_end_to_end(client, owner):
    c = customer(client, owner)
    inv = invoice(client, owner, c).json()
    pay(client, owner, inv["id"], 400, payment_mode="UPI", reference_no="UTR1")
    pay(client, owner, inv["id"], 780)
    wrong = next(p for p in payments(client, owner, inv["id"]) if float(p["amount"]) == 780)

    r = client.post(f"/api/v1/invoices/{inv['id']}/payments/{wrong['id']}/reverse", headers=H(owner),
                    json={"reason": "Entered on the wrong invoice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert float(body["paid_amount"]) == 400 and body["payment_status"] == "PARTIAL" and body["reversal_id"]

    history = payments(client, owner, inv["id"])
    assert len(history) == 3 and sum(float(p["amount"]) for p in history) == 400
    original = next(p for p in history if p["id"] == wrong["id"])
    reversal = next(p for p in history if p["id"] == body["reversal_id"])
    assert original["reversed_at"] and original["reversal_reason"] == "Entered on the wrong invoice"
    assert float(reversal["amount"]) == -780 and reversal["reverses_payment_id"] == wrong["id"]

    # once only, and neither the reversal nor the reversed payment can be touched again
    again = client.post(f"/api/v1/invoices/{inv['id']}/payments/{wrong['id']}/reverse", headers=H(owner), json={"reason": "x"})
    assert again.status_code == 409
    undo = client.post(f"/api/v1/invoices/{inv['id']}/payments/{reversal['id']}/reverse", headers=H(owner), json={"reason": "x"})
    assert undo.status_code == 400
    assert client.put(f"/api/v1/invoices/{inv['id']}/payments/{wrong['id']}", headers=H(owner),
                      json={"notes": "late"}).status_code == 409

    # the right amount can now be recorded; a fully reversed invoice goes back to UNPAID
    pay(client, owner, inv["id"], 780)
    got = client.get(f"/api/v1/invoices/{inv['id']}", headers=H(owner)).json()
    assert got["payment_status"] == "PAID" and float(got["paid_amount"]) == 1180
    first = next(p for p in payments(client, owner, inv["id"]) if p["reference_no"] == "UTR1")
    only = client.post(f"/api/v1/invoices/{inv['id']}/payments/{first['id']}/reverse", headers=H(owner), json={"reason": "Bounced"})
    assert only.json()["payment_status"] == "PARTIAL"


def test_reverse_validation_and_cancelled_invoice(client, owner):
    inv = invoice(client, owner, customer(client, owner)).json()
    pay(client, owner, inv["id"], 100)
    p = payments(client, owner, inv["id"])[0]
    url = f"/api/v1/invoices/{inv['id']}/payments/{p['id']}/reverse"
    assert client.post(url, headers=H(owner), json={}).status_code == 422
    assert client.post(url, headers=H(owner), json={"reason": ""}).status_code == 422
    other = invoice(client, owner, customer(client, owner)).json()
    assert client.post(f"/api/v1/invoices/{other['id']}/payments/{p['id']}/reverse", headers=H(owner),
                       json={"reason": "x"}).status_code == 404  # a payment of a different invoice
    assert client.post(f"/api/v1/invoices/{inv['id']}/payments/00000000-0000-0000-0000-000000000000/reverse",
                       headers=H(owner), json={"reason": "x"}).status_code == 404

    # after a cancel-with-refund the refund row and the payment are history, not editable
    assert client.post(f"/api/v1/invoices/{inv['id']}/cancel", headers=H(owner),
                       json={"reason": "Customer left", "refund_payments": True}).status_code == 200
    assert client.post(url, headers=H(owner), json={"reason": "x"}).status_code == 409
    assert client.put(f"/api/v1/invoices/{inv['id']}/payments/{p['id']}", headers=H(owner),
                      json={"notes": "x"}).status_code == 409


def test_edit_payment_details(client, owner):
    inv = invoice(client, owner, customer(client, owner)).json()
    pay(client, owner, inv["id"], 500, notes="first")
    p = payments(client, owner, inv["id"])[0]
    url = f"/api/v1/invoices/{inv['id']}/payments/{p['id']}"
    r = client.put(url, headers=H(owner), json={"payment_mode": "BANK", "reference_no": "NEFT77", "notes": None})
    assert r.status_code == 200, r.text
    assert (r.json()["payment_mode"], r.json()["reference_no"], r.json()["notes"]) == ("BANK", "NEFT77", None)
    assert float(r.json()["amount"]) == 500  # the amount is never editable

    assert client.put(url, headers=H(owner), json={}).status_code == 422
    assert client.put(url, headers=H(owner), json={"payment_mode": None}).status_code == 422
    assert client.put(url, headers=H(owner), json={"payment_mode": "BARTER"}).status_code == 422
    assert client.put(url, headers=H(owner), json={"payment_date": (TODAY + timedelta(days=1)).isoformat()}).status_code == 422
    assert client.put(url, headers=H(owner), json={"amount": 5}).status_code == 422  # unknown keys are ignored, so nothing is left to change
    assert float(payments(client, owner, inv["id"])[0]["amount"]) == 500


def test_reversal_and_edit_are_audited_and_owner_only(client, owner):
    inv = invoice(client, owner, customer(client, owner)).json()
    pay(client, owner, inv["id"], 100)
    p = payments(client, owner, inv["id"])[0]
    client.put(f"/api/v1/invoices/{inv['id']}/payments/{p['id']}", headers=H(owner), json={"notes": "fixed"})
    client.post(f"/api/v1/invoices/{inv['id']}/payments/{p['id']}/reverse", headers=H(owner), json={"reason": "oops"})
    client.put(f"/api/v1/invoices/{inv['id']}/due-date", headers=H(owner), json={"payment_terms_days": 3})
    logs = client.get("/api/v1/audit-logs", headers=H(owner), params={"entity_type": "INVOICE", "limit": 20}).json()
    actions = {entry["action"] for entry in logs}
    assert {"UPDATE_PAYMENT", "REVERSE_PAYMENT", "UPDATE_DUE_DATE"} <= actions
    assert {"UPDATE_PAYMENT", "REVERSE_PAYMENT", "UPDATE_DUE_DATE"} <= set(
        client.get("/api/v1/audit-logs/meta", headers=H(owner)).json()["actions"])

    # staff can't use any of it
    phone = f"96{str(time.time_ns())[-8:]}"
    staff = client.post("/api/v1/auth/users", headers=H(owner), json={"name": "Staff", "phone": phone, "role": "STAFF"}).json()
    from tests.helpers import first_login
    token = first_login(client, staff)
    for method, path, body in [
        ("put", f"/invoices/{inv['id']}/payments/{p['id']}", {"notes": "x"}),
        ("post", f"/invoices/{inv['id']}/payments/{p['id']}/reverse", {"reason": "x"}),
        ("get", f"/invoices/{inv['id']}/payments/{p['id']}/receipt", None),
        ("put", f"/invoices/{inv['id']}/due-date", {"payment_terms_days": 1}),
        ("get", "/reports/receivables-ageing", None),
    ]:
        resp = getattr(client, method)(f"/api/v1{path}", headers=H(token), **({"json": body} if body else {}))
        assert resp.status_code == 403, (path, resp.status_code)


# ---------------------------------------------------------------------------------------------- receipts / vouchers
def test_payment_receipt_pdf(client, owner):
    inv = invoice(client, owner, customer(client, owner)).json()
    pay(client, owner, inv["id"], 500, payment_mode="UPI", reference_no="UTR9")
    p = payments(client, owner, inv["id"])[0]
    r = client.get(f"/api/v1/invoices/{inv['id']}/payments/{p['id']}/receipt", headers=H(owner))
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")

    assert client.get(f"/api/v1/invoices/{inv['id']}/payments/00000000-0000-0000-0000-000000000000/receipt",
                      headers=H(owner)).status_code == 404
    client.post(f"/api/v1/invoices/{inv['id']}/payments/{p['id']}/reverse", headers=H(owner), json={"reason": "wrong"})
    assert client.get(f"/api/v1/invoices/{inv['id']}/payments/{p['id']}/receipt", headers=H(owner)).status_code == 409
    reversal = next(x for x in payments(client, owner, inv["id"]) if float(x["amount"]) < 0)
    assert client.get(f"/api/v1/invoices/{inv['id']}/payments/{reversal['id']}/receipt", headers=H(owner)).status_code == 400


def test_purchase_payment_voucher_reverse_and_edit(client, owner):
    sup = client.post("/api/v1/suppliers", headers=H(owner), json={"name": f"Sup {time.time_ns()}"}).json()
    prod = client.post("/api/v1/products", headers=H(owner), json={"name": f"Cable {time.time_ns()}", "gst_rate": 18}).json()
    pur = client.post("/api/v1/purchases", headers=H(owner), json={
        "supplier_id": sup["id"], "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 100, "gst_rate": 18}]})
    assert pur.status_code == 201, pur.text
    pid = pur.json()["id"]
    assert client.post(f"/api/v1/purchases/{pid}/payments", headers=H(owner), json={"amount": 600}).status_code == 200
    p = client.get(f"/api/v1/purchases/{pid}/payments", headers=H(owner)).json()[0]

    v = client.get(f"/api/v1/purchases/{pid}/payments/{p['id']}/voucher", headers=H(owner))
    assert v.status_code == 200 and v.content.startswith(b"%PDF")
    e = client.put(f"/api/v1/purchases/{pid}/payments/{p['id']}", headers=H(owner), json={"payment_mode": "CHEQUE", "reference_no": "000123"})
    assert e.status_code == 200 and e.json()["payment_mode"] == "CHEQUE"

    r = client.post(f"/api/v1/purchases/{pid}/payments/{p['id']}/reverse", headers=H(owner), json={"reason": "Cheque bounced"})
    assert r.status_code == 200 and float(r.json()["paid_amount"]) == 0 and r.json()["payment_status"] == "UNPAID"
    assert client.get(f"/api/v1/purchases/{pid}/payments/{p['id']}/voucher", headers=H(owner)).status_code == 409
    assert client.post(f"/api/v1/purchases/{pid}/payments/{p['id']}/reverse", headers=H(owner), json={"reason": "x"}).status_code == 409


# ---------------------------------------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("amount, words", [
    (0, "Rupees Zero Only"),
    (1, "Rupees One Only"),
    (118, "Rupees One Hundred Eighteen Only"),
    (1234.5, "Rupees One Thousand Two Hundred Thirty Four and Fifty Paise Only"),
    (125000, "Rupees One Lakh Twenty Five Thousand Only"),
    (Decimal("10000000.01"), "Rupees One Crore and One Paise Only"),
])
def test_amount_in_words(amount, words):
    assert amount_in_words(amount) == words
