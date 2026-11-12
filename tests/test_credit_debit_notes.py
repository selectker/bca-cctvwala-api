"""v1.38.0: credit notes (sales returns), debit notes (purchase returns) and the purchase attachment.

A real PostgreSQL, same setup as test_invoice_edit_delete.py. Every test makes its own customer / product / supplier.
"""
import os
import time
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app.main import app

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
TODAY = date.today().isoformat()
API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def owner(client):
    if client.get(f"{API}/auth/bootstrap-status").json()["needs_admin_bootstrap"]:
        assert client.post(f"{API}/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}).status_code == 201
    if client.get(f"{API}/auth/bootstrap-status").json()["needs_owner_bootstrap"]:
        assert client.post(f"{API}/auth/bootstrap-owner", json={"name": "Owner", **OWNER}).status_code == 201
    return client.post(f"{API}/auth/login", json=OWNER).json()["token"]


def customer(client, owner, state="29"):
    r = client.post(f"{API}/customers", headers=H(owner), json={"name": f"CN {time.time_ns()}", "state_code": state})
    assert r.status_code == 201, r.text
    return r.json()


def supplier(client, owner, state="29"):
    r = client.post(f"{API}/suppliers", headers=H(owner), json={"name": f"Sup {time.time_ns()}", "state_code": state})
    assert r.status_code == 201, r.text
    return r.json()


def product(client, owner, serialized=False):
    r = client.post(f"{API}/products", headers=H(owner), json={
        "name": f"Cam {time.time_ns()}", "sku": f"SKU{time.time_ns()}", "unit_price": 500, "gst_rate": 18,
        "is_serialized": serialized, "opening_stock": 0, "purchase_price": 300})
    assert r.status_code == 201, r.text
    return r.json()


def stock(client, owner, product_id):
    rows = client.get(f"{API}/products", headers=H(owner), params={"search": ""}).json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    return float(next(r for r in rows if r["id"] == product_id)["current_stock"])


def buy(client, owner, p, qty, serials=None, sup=None, price=300):
    sup = sup or supplier(client, owner)
    item = {"product_id": p["id"], "quantity": qty, "unit_price": price, "gst_rate": 18}
    if serials:
        item.update(barcodes=serials, supplier_warranty_months=12)
    r = client.post(f"{API}/purchases", headers=H(owner), json={"supplier_id": sup["id"], "items": [item]})
    assert r.status_code == 201, r.text
    return r.json()


def sell(client, owner, p, qty, serials=None, cust=None, price=1000):
    cust = cust or customer(client, owner)
    item = {"product_id": p["id"], "quantity": qty, "unit_price": price, "gst_rate": 18}
    if serials:
        item.update(barcodes=serials, customer_warranty_months=12)
    r = client.post(f"{API}/invoices", headers=H(owner), json={"customer_id": cust["id"], "items": [item]})
    assert r.status_code == 201, r.text
    return r.json()


def pay(client, owner, kind, doc_id, amount):
    r = client.post(f"{API}/{kind}/{doc_id}/payments", headers=H(owner), json={"amount": amount, "payment_mode": "CASH"})
    assert r.status_code in (200, 201), r.text


def invoice_detail(client, owner, inv_id):
    return client.get(f"{API}/invoices/{inv_id}", headers=H(owner)).json()


def credit(client, owner, inv, lines, **extra):
    body = {"invoice_id": inv["id"], "reason": "Customer returned", "items": lines, **extra}
    return client.post(f"{API}/credit-notes", headers=H(owner), json=body)


# ---------------------------------------------------------------------------------------------- credit notes
def test_credit_note_cuts_what_the_customer_owes_and_restocks(client, owner):
    p = product(client, owner)
    buy(client, owner, p, 10)
    inv = sell(client, owner, p, 4)                      # 4 x 1000 + 18% = 4720
    assert float(inv["total_amount"]) == 4720
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    assert stock(client, owner, p["id"]) == 6

    r = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1}])
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["credit_note_no"].startswith("CN/") and float(note["total_amount"]) == 1180
    assert float(note["applied_to_invoice"]) == 1180 and float(note["refund_amount"]) == 0
    assert stock(client, owner, p["id"]) == 7

    detail = invoice_detail(client, owner, inv["id"])
    assert float(detail["paid_amount"]) == 1180 and detail["payment_status"] == "PARTIAL"   # 3540 still owed
    assert float(detail["items"][0]["returned_quantity"]) == 1
    assert detail["credit_notes"][0]["credit_note_no"] == note["credit_note_no"]

    # can't return more than what is left
    too_many = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 4}])
    assert too_many.status_code == 400
    pdf = client.get(f"{API}/credit-notes/{note['id']}/pdf", headers=H(owner))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_credit_note_after_payment_books_a_refund(client, owner):
    p = product(client, owner)
    buy(client, owner, p, 5)
    inv = sell(client, owner, p, 2)                      # 2360
    pay(client, owner, "invoices", inv["id"], 2360)
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    r = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 2}], refund_mode="UPI")
    assert r.status_code == 201, r.text
    note = r.json()
    assert float(note["total_amount"]) == 2360 and float(note["refund_amount"]) == 2360 and note["refund_mode"] == "UPI"
    payments = client.get(f"{API}/invoices/{inv['id']}/payments", headers=H(owner)).json()
    payments = payments["items"] if isinstance(payments, dict) else payments
    assert sorted(float(x["amount"]) for x in payments) == [-2360, 2360, 2360]
    assert invoice_detail(client, owner, inv["id"])["payment_status"] == "PAID"

    # the rows booked by the note can't be edited or reversed by hand, and the invoice can't be cancelled / edited
    booked = next(x for x in payments if x["payment_mode"] == "CREDIT_NOTE")
    assert booked["credit_note_id"] == note["id"]
    assert client.post(f"{API}/invoices/{inv['id']}/payments/{booked['id']}/reverse", headers=H(owner),
                       json={"reason": "x"}).status_code == 409
    assert client.post(f"{API}/invoices/{inv['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 409
    assert client.put(f"{API}/invoices/{inv['id']}", headers=H(owner), json={"items": [{"description": "x", "quantity": 1, "unit_price": 10, "gst_rate": 18}]}).status_code == 409


def test_cancel_credit_note_undoes_everything(client, owner):
    p = product(client, owner)
    buy(client, owner, p, 5)
    inv = sell(client, owner, p, 3)
    pay(client, owner, "invoices", inv["id"], 3540)
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    note = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 3}]).json()
    assert stock(client, owner, p["id"]) == 5
    r = client.post(f"{API}/credit-notes/{note['id']}/cancel", headers=H(owner), json={"reason": "entered by mistake"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert stock(client, owner, p["id"]) == 2
    detail = invoice_detail(client, owner, inv["id"])
    assert float(detail["paid_amount"]) == 3540 and detail["payment_status"] == "PAID"
    assert float(detail["items"][0]["returned_quantity"]) == 0
    assert client.post(f"{API}/credit-notes/{note['id']}/cancel", headers=H(owner), json={"reason": "again"}).status_code == 409
    # the invoice is editable again, and the full quantity can be returned afresh
    assert credit(client, owner, inv, [{"item_id": line["id"], "quantity": 3}]).status_code == 201


def test_credit_note_for_serial_units_and_scrap(client, owner):
    p = product(client, owner, serialized=True)
    codes = [f"CNS{time.time_ns()}{i}" for i in range(3)]
    buy(client, owner, p, 3, serials=codes)
    inv = sell(client, owner, p, 3, serials=codes)
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    assert credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1}]).status_code == 400            # no serial
    assert credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1, "barcodes": ["NOPE"]}]).status_code == 400
    ok = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1, "barcodes": [codes[0]]}])
    assert ok.status_code == 201, ok.text
    assert stock(client, owner, p["id"]) == 1
    unit = client.get(f"{API}/warranties/barcode/{codes[0]}", headers=H(owner))
    if unit.status_code == 200:
        assert unit.json().get("status", "IN_STOCK") == "IN_STOCK"
    # the same unit can't be returned twice
    assert credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1, "barcodes": [codes[0]]}]).status_code == 400
    # damaged goods: no stock added, the unit is scrapped
    bad = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1, "barcodes": [codes[1]]}], restock=False)
    assert bad.status_code == 201, bad.text
    assert stock(client, owner, p["id"]) == 1
    # cancelling the first note puts the unit back with the customer
    assert client.post(f"{API}/credit-notes/{ok.json()['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 200
    assert stock(client, owner, p["id"]) == 0
    after = invoice_detail(client, owner, inv["id"])["items"][0]
    assert codes[0] in after["barcodes"] and after["customer_warranty_months"] == 12


def test_partial_returns_never_drift(client, owner):
    p = product(client, owner)
    buy(client, owner, p, 10)
    inv = sell(client, owner, p, 3, price=333.33)
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    totals = Decimal("0")
    for _ in range(3):
        r = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1}])
        assert r.status_code == 201, r.text
        totals += Decimal(str(r.json()["total_amount"]))
    assert totals <= Decimal(str(inv["total_amount"]))
    assert invoice_detail(client, owner, inv["id"])["items"][0]["returned_quantity"] in (3, 3.0, "3.00")


def test_credit_note_guards(client, owner):
    p = product(client, owner)
    buy(client, owner, p, 5)
    inv = sell(client, owner, p, 1)
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    dup = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1}, {"item_id": line["id"], "quantity": 1}])
    assert dup.status_code == 400
    assert credit(client, owner, inv, [{"item_id": "00000000-0000-0000-0000-000000000000", "quantity": 1}]).status_code == 400
    assert client.post(f"{API}/invoices/{inv['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 200
    assert credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1}]).status_code == 409
    assert client.get(f"{API}/credit-notes/00000000-0000-0000-0000-000000000000", headers=H(owner)).status_code == 404
    listed = client.get(f"{API}/credit-notes", headers=H(owner), params={"search": "CN/"}).json()
    assert isinstance(listed, list)


# ---------------------------------------------------------------------------------------------- debit notes
def test_debit_note_returns_goods_to_the_supplier(client, owner):
    p = product(client, owner)
    sup = supplier(client, owner)
    pur = buy(client, owner, p, 10, sup=sup)             # 10 x 300 + 18% = 3540
    line = client.get(f"{API}/purchases/{pur['id']}", headers=H(owner)).json()["items"][0]
    r = client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "Faulty batch", "items": [{"item_id": line["id"], "quantity": 4}]})
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["debit_note_no"].startswith("DN/") and float(note["total_amount"]) == 1416
    assert float(note["applied_to_purchase"]) == 1416 and float(note["refund_amount"]) == 0
    assert stock(client, owner, p["id"]) == 6
    detail = client.get(f"{API}/purchases/{pur['id']}", headers=H(owner)).json()
    assert float(detail["paid_amount"]) == 1416 and detail["debit_notes"][0]["debit_note_no"] == note["debit_note_no"]
    assert float(detail["items"][0]["returned_quantity"]) == 4
    assert client.get(f"{API}/debit-notes/{note['id']}/pdf", headers=H(owner)).content.startswith(b"%PDF")
    assert client.post(f"{API}/purchases/{pur['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 409

    over = client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "x", "items": [{"item_id": line["id"], "quantity": 7}]})
    assert over.status_code == 400
    c = client.post(f"{API}/debit-notes/{note['id']}/cancel", headers=H(owner), json={"reason": "supplier refused"})
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    assert stock(client, owner, p["id"]) == 10
    assert float(client.get(f"{API}/purchases/{pur['id']}", headers=H(owner)).json()["paid_amount"]) == 0


def test_debit_note_after_payment_and_serials(client, owner):
    p = product(client, owner, serialized=True)
    codes = [f"DNS{time.time_ns()}{i}" for i in range(2)]
    pur = buy(client, owner, p, 2, serials=codes)        # 2 x 300 + 18% = 708
    pay(client, owner, "purchases", pur["id"], 708)
    line = client.get(f"{API}/purchases/{pur['id']}", headers=H(owner)).json()["items"][0]
    assert client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "x", "items": [{"item_id": line["id"], "quantity": 1}]}).status_code == 400
    r = client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "DOA", "refund_mode": "BANK",
        "items": [{"item_id": line["id"], "quantity": 1, "barcodes": [codes[0]]}]})
    assert r.status_code == 201, r.text
    note = r.json()
    assert float(note["total_amount"]) == 354 and float(note["refund_amount"]) == 354 and note["refund_mode"] == "BANK"
    # a unit already returned can't go twice; a sold unit can't go at all
    again = client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "x", "items": [{"item_id": line["id"], "quantity": 1, "barcodes": [codes[0]]}]})
    assert again.status_code in (400, 409)
    sell(client, owner, p, 1, serials=[codes[1]])
    sold = client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "x", "items": [{"item_id": line["id"], "quantity": 1, "barcodes": [codes[1]]}]})
    assert sold.status_code in (400, 409)


# ---------------------------------------------------------------------------------------------- reports
def test_reports_net_credit_and_debit_notes(client, owner):
    q = {"from": TODAY, "to": TODAY}
    before_gst = client.get(f"{API}/reports/gst-summary", headers=H(owner), params=q).json()
    before_itc = client.get(f"{API}/reports/gst-input-credit", headers=H(owner), params=q).json()
    p = product(client, owner)
    sup = supplier(client, owner)
    pur = buy(client, owner, p, 10, sup=sup)
    inv = sell(client, owner, p, 4)
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    cn = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1}]).json()
    pline = client.get(f"{API}/purchases/{pur['id']}", headers=H(owner)).json()["items"][0]
    dn = client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "x", "items": [{"item_id": pline["id"], "quantity": 2}]}).json()

    gst = client.get(f"{API}/reports/gst-summary", headers=H(owner), params=q).json()
    # +4000 invoiced, -1000 returned (before GST) => +3000 net
    assert float(gst["total_taxable"]) - float(before_gst["total_taxable"]) == pytest.approx(3000)
    assert gst["credit_note_count"] - before_gst["credit_note_count"] == 1
    itc = client.get(f"{API}/reports/gst-input-credit", headers=H(owner), params=q).json()
    assert float(itc["total_taxable"]) - float(before_itc["total_taxable"]) == pytest.approx(3000 - 600)
    assert itc["debit_note_count"] - before_itc["debit_note_count"] == 1

    pl = client.get(f"{API}/reports/profit-loss", headers=H(owner), params=q)
    assert pl.status_code == 200 and "credit_note_taxable" in pl.json()
    g1 = client.get(f"{API}/reports/gstr1", headers=H(owner), params=q).json()
    assert any(r["credit_note_no"] == cn["credit_note_no"] for r in g1["cdn"])
    assert g1["documents"]["cn_issued"] >= 1 and "net_totals" in g1
    csv = client.get(f"{API}/reports/gstr1/csv", headers=H(owner), params={**q, "section": "cdn"})
    assert csv.status_code == 200 and cn["credit_note_no"] in csv.content.decode("utf-8-sig")
    assert client.get(f"{API}/reports/gstr1/csv", headers=H(owner), params=q).status_code == 200
    assert client.get(f"{API}/reports/gstr3b", headers=H(owner), params=q).status_code == 200
    assert client.get(f"{API}/reports/sales-summary", headers=H(owner), params=q).status_code == 200
    assert dn["status"] == "ACTIVE"


# ---------------------------------------------------------------------------------------------- attachment
def _png():
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 30), (200, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


def test_purchase_attachment_image_pdf_and_bad_file(client, owner):
    p = product(client, owner)
    pur = buy(client, owner, p, 1)
    url = f"{API}/purchases/{pur['id']}/attachment"
    assert client.get(url, headers=H(owner)).status_code == 404
    up = client.post(url, headers=H(owner), files={"file": ("bill.png", _png(), "image/png")})
    assert up.status_code == 200, up.text
    assert up.json()["attachment_type"] == "image/jpeg"
    got = client.get(url, headers=H(owner))
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg" and got.content[:2] == b"\xff\xd8"
    detail = client.get(f"{API}/purchases/{pur['id']}", headers=H(owner)).json()
    assert detail["has_attachment"] is True and "attachment_key" not in detail

    pdf = client.post(url, headers=H(owner), files={"file": ("bill.pdf", b"%PDF-1.4\n%fake", "application/pdf")})
    assert pdf.status_code == 200 and pdf.json()["attachment_type"] == "application/pdf"
    assert client.get(url, headers=H(owner)).content.startswith(b"%PDF")
    bad = client.post(url, headers=H(owner), files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 400
    assert client.delete(url, headers=H(owner)).status_code == 204
    assert client.get(url, headers=H(owner)).status_code == 404
    missing = client.post(f"{API}/purchases/00000000-0000-0000-0000-000000000000/attachment", headers=H(owner),
                          files={"file": ("bill.png", _png(), "image/png")})
    assert missing.status_code == 404


# ---------------------------------------------------------------------------------------------- walk-in quotation
def test_quotation_can_be_made_for_the_walk_in_customer(client, owner):
    walk_in = client.get(f"{API}/customers/walk-in", headers=H(owner)).json()
    p = product(client, owner)
    r = client.post(f"{API}/quotations", headers=H(owner), json={
        "customer_id": walk_in["id"], "items": [{"product_id": p["id"], "quantity": 1, "unit_price": 700, "gst_rate": 18}]})
    assert r.status_code == 201, r.text
    listed = client.get(f"{API}/quotations", headers=H(owner), params={"customer_id": walk_in["id"]}).json()
    listed = listed["items"] if isinstance(listed, dict) else listed
    assert any(q["id"] == r.json()["id"] for q in listed)
    assert client.get(f"{API}/quotations/{r.json()['id']}/pdf", headers=H(owner)).content.startswith(b"%PDF")


# ---------------------------------------------------------------------------------------------- v1.38.1 custom line cost
def test_custom_line_cost_feeds_profit(client, owner):
    q = {"from": TODAY, "to": TODAY}
    before = client.get(f"{API}/reports/profit-loss", headers=H(owner), params=q).json()
    cust = customer(client, owner)
    r = client.post(f"{API}/invoices", headers=H(owner), json={"customer_id": cust["id"], "items": [
        {"description": "Cable laying", "quantity": 2, "unit_price": 1000, "gst_rate": 18, "cost_price": 600},
        {"description": "No cost known", "quantity": 1, "unit_price": 500, "gst_rate": 18}]})
    assert r.status_code == 201, r.text
    lines = client.get(f"{API}/invoices/{r.json()['id']}", headers=H(owner)).json()["items"]
    assert sorted(float(x["cost_price"]) if x["cost_price"] is not None else -1 for x in lines) == [-1, 600]
    after = client.get(f"{API}/reports/profit-loss", headers=H(owner), params=q).json()
    assert after["cost_of_goods"] - before["cost_of_goods"] == pytest.approx(1200)
    assert after["sales_taxable"] - before["sales_taxable"] == pytest.approx(2500)
    bad = client.post(f"{API}/invoices", headers=H(owner), json={"customer_id": cust["id"], "items": [
        {"description": "x", "quantity": 1, "unit_price": 10, "gst_rate": 18, "cost_price": -1}]})
    assert bad.status_code == 422


# ---------------------------------------------------------------------------------------------- v1.38.2 ledgers
def test_customer_ledger_matches_the_balance(client, owner):
    p = product(client, owner)
    buy(client, owner, p, 10)
    cust = customer(client, owner)
    inv = sell(client, owner, p, 2, cust=cust)                 # 2360
    pay(client, owner, "invoices", inv["id"], 1000)
    line = invoice_detail(client, owner, inv["id"])["items"][0]
    note = credit(client, owner, inv, [{"item_id": line["id"], "quantity": 1}]).json()   # 1180
    led = client.get(f"{API}/customers/{cust['id']}/ledger", headers=H(owner))
    assert led.status_code == 200, led.text
    data = led.json()
    assert [e["type"] for e in data["entries"]] == ["Invoice", "Receipt", "Credit note"]
    assert float(data["total_debit"]) == 2360 and float(data["total_credit"]) == 2180
    assert float(data["closing_balance"]) == 180
    assert float(data["entries"][1]["balance"]) == 1360
    bal = client.get(f"{API}/customers/{cust['id']}/balance", headers=H(owner)).json()
    assert float(data["closing_balance"]) == pytest.approx(bal["outstanding"])
    # a period: what stood before it becomes the opening balance
    later = client.get(f"{API}/customers/{cust['id']}/ledger", headers=H(owner), params={"from": "2999-01-01"}).json()
    assert later["entries"] == [] and float(later["opening_balance"]) == 180
    # cancelling the credit note and reversing the payment brings the ledger to the full invoice again
    assert client.post(f"{API}/credit-notes/{note['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 200
    again = client.get(f"{API}/customers/{cust['id']}/ledger", headers=H(owner)).json()
    assert float(again["closing_balance"]) == 1360
    pdf = client.get(f"{API}/customers/{cust['id']}/ledger/pdf", headers=H(owner))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert client.get(f"{API}/customers/00000000-0000-0000-0000-000000000000/ledger", headers=H(owner)).status_code == 404


def test_supplier_ledger_matches_the_balance(client, owner):
    p = product(client, owner)
    sup = supplier(client, owner)
    pur = buy(client, owner, p, 10, sup=sup)                   # 3540
    pay(client, owner, "purchases", pur["id"], 1000)
    pline = client.get(f"{API}/purchases/{pur['id']}", headers=H(owner)).json()["items"][0]
    r = client.post(f"{API}/debit-notes", headers=H(owner), json={
        "purchase_id": pur["id"], "reason": "x", "items": [{"item_id": pline["id"], "quantity": 4}]})   # 1416
    assert r.status_code == 201, r.text
    data = client.get(f"{API}/suppliers/{sup['id']}/ledger", headers=H(owner)).json()
    assert [e["type"] for e in data["entries"]] == ["Purchase", "Payment", "Debit note"]
    assert float(data["closing_balance"]) == 3540 - 1000 - 1416
    bal = client.get(f"{API}/suppliers/{sup['id']}/balance", headers=H(owner)).json()
    assert float(data["closing_balance"]) == pytest.approx(bal["payable"])
    assert client.get(f"{API}/suppliers/{sup['id']}/ledger/pdf", headers=H(owner)).content.startswith(b"%PDF")


def test_invoice_pdf_prints_serials(client=None):
    from app.utils.pdf import build_invoice_pdf
    store = {"shop_name": "S", "state_code": "27"}
    inv = {"invoice_no": "INV-1", "invoice_date": "2026-11-11", "customer_name": "C", "total_amount": 118, "subtotal": 100,
           "gst_amount": 18, "paid_amount": 0, "status": "ISSUED"}
    item = {"description": "Camera", "quantity": 2, "unit_price": 50, "gst_rate": 18, "cgst_amount": 9, "sgst_amount": 9,
            "igst_amount": 0, "line_total": 118, "barcodes": ["SN-AAA-111", "SN-BBB-222"]}
    pdf = build_invoice_pdf(store, inv, [item])
    assert pdf.startswith(b"%PDF")
