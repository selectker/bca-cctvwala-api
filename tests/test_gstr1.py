"""v1.19.0: HSN/SAC on invoice lines and PDFs, and the GSTR-1 / GSTR-3B style reports with CSV downloads.

The pure helpers are tested without a database; the end-to-end tests need the same real PostgreSQL as the others and
find their own rows by unique names / HSN codes, so other tests' invoices don't matter.
"""
import io
import os
import shutil
import subprocess
import time
import zipfile
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils import gstr
from app.utils.pdf import build_invoice_pdf, build_quotation_pdf
from tests.helpers import first_login

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
TODAY = date.today().isoformat()


# ------------------------------------------------------------------ pure helpers
def _rate_row(**kw):
    base = {"invoice_no": "INV/1", "invoice_date": date(2026, 10, 1), "total_amount": Decimal("1180"),
            "place_of_supply_state_code": "29", "is_interstate": False, "customer_name": "Asha", "customer_gstin": "",
            "gst_rate": Decimal("18"), "taxable": Decimal("1000"), "cgst": Decimal("90"), "sgst": Decimal("90"), "igst": Decimal("0")}
    return {**base, **kw}


def test_sections_split_registered_large_and_small_sales():
    rows = [
        _rate_row(invoice_no="INV/1", customer_gstin="29ABCDE1234F1Z5"),
        _rate_row(invoice_no="INV/2"),
        _rate_row(invoice_no="INV/3"),
        _rate_row(invoice_no="INV/4", is_interstate=True, place_of_supply_state_code="27", total_amount=Decimal("150000"),
                  taxable=Decimal("127118.64"), cgst=Decimal("0"), sgst=Decimal("0"), igst=Decimal("22881.36")),
        _rate_row(invoice_no="INV/5", is_interstate=True, place_of_supply_state_code="27", total_amount=Decimal("100000"),
                  taxable=Decimal("84745.76"), cgst=Decimal("0"), sgst=Decimal("0"), igst=Decimal("15254.24")),
    ]
    s = gstr.build_sales_sections(rows, "29")
    assert [r["invoice_no"] for r in s["b2b"]] == ["INV/1"]
    assert [r["invoice_no"] for r in s["b2cl"]] == ["INV/4"]  # exactly 1,00,000 is not "above" the limit
    small = {(r["place_of_supply_code"], r["rate"]): r for r in s["b2cs"]}
    assert small[("29", Decimal("18"))]["taxable"] == Decimal("2000.00")  # INV/2 + INV/3 added together
    assert small[("27", Decimal("18"))]["igst"] == Decimal("15254.24")
    assert small[("29", Decimal("18"))]["type"] == "OE"
    assert s["rate_summary"][0]["taxable"] == Decimal("214864.40")


def test_a_sale_with_no_place_of_supply_counts_as_the_stores_own_state():
    s = gstr.build_sales_sections([_rate_row(place_of_supply_state_code=None)], "29")
    assert s["b2cs"][0]["place_of_supply"] == "29-Karnataka"


def test_documents_summary_counts_cancelled():
    docs = gstr.build_document_summary([{"invoice_no": "A1", "status": "ACTIVE"}, {"invoice_no": "A2", "status": "CANCELLED"},
                                         {"invoice_no": "A3", "status": "ACTIVE"}])
    assert docs == {"from_no": "A1", "to_no": "A3", "total_issued": 3, "cancelled": 1, "net_issued": 2}
    assert gstr.build_document_summary([])["total_issued"] == 0


def test_csv_neutralises_spreadsheet_formulas_and_keeps_numbers():
    data = gstr.to_csv(["Name", "Amount"], [["=HYPERLINK(\"http://x\")", Decimal("-5")], ["+91 98", 12], ["Ravi", None]])
    assert data.startswith(b"\xef\xbb\xbf")  # byte-order mark, so Excel reads it as UTF-8
    text = data.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "'+91 98" in text
    assert ",-5.00" in text  # a real negative number is not treated as text
    assert text.splitlines()[3] == "Ravi,"  # a missing value is just an empty cell


# ------------------------------------------------------------------ PDFs
STORE = {"store_name": "Test Store", "address": "1 Main Rd", "phone": "99999"}
INVOICE = {"invoice_no": "INV/2026-27/0001", "invoice_date": date(2026, 10, 1), "customer_name": "Asha", "subtotal": 1000,
           "total_cgst": 90, "total_sgst": 90, "total_igst": 0, "round_off": 0, "total_amount": 1180, "paid_amount": 0, "status": "ACTIVE"}


def _pdf_text(pdf: bytes) -> str:
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, check=True).stdout.decode()


def _line(**kw):
    return {"description": None, "product_name": "4ch DVR", "quantity": 1, "unit_price": 1000, "gst_rate": 18,
            "cgst_amount": 90, "sgst_amount": 90, "igst_amount": 0, "line_total": 1180, **kw}


def test_invoice_pdf_prints_the_hsn_of_each_line():
    pdf = build_invoice_pdf(STORE, INVOICE, [_line(hsn_code="85258010"), _line(product_name=None, description="Installation", hsn_code="998719"),
                                              _line(product_name="Old row", product_hsn_code="85444299"), _line(product_name="No code")])
    text = _pdf_text(pdf)
    assert "HSN/SAC" in text
    for code in ("85258010", "998719", "85444299"):
        assert code in text


def test_stored_hsn_wins_over_the_products_current_one():
    text = _pdf_text(build_invoice_pdf(STORE, INVOICE, [_line(hsn_code="11111111", product_hsn_code="22222222")]))
    assert "11111111" in text and "22222222" not in text


def test_long_item_name_is_cut_before_the_hsn_column():
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from app.utils.pdf import _DESC_WIDTH, _fit

    assert stringWidth(_fit("W" * 80, "Helvetica", 9, _DESC_WIDTH), "Helvetica", 9) <= _DESC_WIDTH
    pdf = build_invoice_pdf(STORE, INVOICE, [_line(product_name="W" * 80, hsn_code="85258010")])
    assert "85258010" in _pdf_text(pdf)


def test_quotation_pdf_shows_the_products_hsn():
    quotation = {"quotation_no": "QUO/1", "quotation_date": date(2026, 10, 1), "subtotal": 1000, "total_amount": 1180,
                 "total_gst": 180, "customer_name": "Asha", "status": "PENDING"}
    assert "85258010" in _pdf_text(build_quotation_pdf(STORE, quotation, [_line(product_hsn_code="85258010")]))


# ------------------------------------------------------------------ end to end
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
def world(client, owner):
    """Two products (one with an HSN), a registered buyer, an unregistered buyer, and an invoice for each."""
    tag = str(time.time_ns())[-9:]
    hsn = f"85{tag[-6:]}"  # unique 8-digit code, so rows from other tests can't mix in
    state = (client.get("/api/v1/store-profile", headers=H(owner)).json().get("state_code")) or "29"
    p1 = client.post("/api/v1/products", headers=H(owner), json={"name": f"HSN Camera {tag}", "hsn_code": hsn, "gst_rate": 18,
                                                                  "selling_price": 1000, "unit": "PCS"})
    p2 = client.post("/api/v1/products", headers=H(owner), json={"name": f"No HSN Cable {tag}", "gst_rate": 18, "selling_price": 100, "unit": "MTR"})
    assert p1.status_code == 201 and p2.status_code == 201, (p1.text, p2.text)
    p1, p2 = p1.json(), p2.json()
    b2b = client.post("/api/v1/customers", headers=H(owner), json={"name": f"Reg Buyer {tag}", "gstin": f"{state}ABCDE1234F1Z5", "state_code": state})
    b2c = client.post("/api/v1/customers", headers=H(owner), json={"name": f"=Walk-in {tag}", "state_code": state})
    assert b2b.status_code == 201 and b2c.status_code == 201, (b2b.text, b2c.text)
    b2b, b2c = b2b.json(), b2c.json()
    inv1 = client.post("/api/v1/invoices", headers=H(owner), json={
        "customer_id": b2b["id"], "allow_negative_stock": True,
        "items": [{"product_id": p1["id"], "quantity": 2, "unit_price": 1000, "gst_rate": 18},
                  {"description": "Installation", "quantity": 1, "unit_price": 500, "gst_rate": 18, "hsn_code": "998719"}]})
    inv2 = client.post("/api/v1/invoices", headers=H(owner), json={
        "customer_id": b2c["id"], "allow_negative_stock": True,
        "items": [{"product_id": p2["id"], "quantity": 3, "unit_price": 100, "gst_rate": 18}]})
    assert inv1.status_code == 201 and inv2.status_code == 201, (inv1.text, inv2.text)
    return {"tag": tag, "hsn": hsn, "p1": p1, "p2": p2, "b2b": b2b, "b2c": b2c, "inv1": inv1.json(), "inv2": inv2.json()}


def report(client, owner, **q):
    r = client.get("/api/v1/reports/gstr1", headers=H(owner), params={"from": TODAY, "to": TODAY, **q})
    assert r.status_code == 200, r.text
    return r.json()


def test_hsn_is_copied_onto_the_invoice_line(client, owner, world):
    inv = client.get(f"/api/v1/invoices/{world['inv1']['id']}", headers=H(owner)).json()
    codes = {i["hsn_code"] for i in inv["items"]}
    assert codes == {world["hsn"], "998719"}
    # changing the product's HSN afterwards leaves the old invoice (and the report) alone
    assert client.put(f"/api/v1/products/{world['p1']['id']}", headers=H(owner), json={"name": world["p1"]["name"], "unit": "PCS", "gst_rate": 18, "selling_price": 1000, "reorder_level": 0, "hsn_code": "99999999"}).status_code == 200
    inv = client.get(f"/api/v1/invoices/{world['inv1']['id']}", headers=H(owner)).json()
    assert {i["hsn_code"] for i in inv["items"]} == {world["hsn"], "998719"}
    assert any(r["hsn_code"] == world["hsn"] for r in report(client, owner)["hsn"])


def test_a_bad_hsn_on_a_line_is_a_422(client, owner, world):
    r = client.post("/api/v1/invoices", headers=H(owner), json={
        "customer_id": world["b2c"]["id"], "items": [{"description": "x", "quantity": 1, "unit_price": 1, "gst_rate": 18, "hsn_code": "ABC"}]})
    assert r.status_code == 422


def test_invoice_pdf_endpoint_shows_the_hsn(client, owner, world):
    r = client.get(f"/api/v1/invoices/{world['inv1']['id']}/pdf", headers=H(owner))
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    text = _pdf_text(r.content)
    assert world["hsn"] in text and "998719" in text
    quote = client.post("/api/v1/quotations", headers=H(owner), json={
        "customer_id": world["b2b"]["id"], "items": [{"product_id": world["p2"]["id"], "quantity": 1, "unit_price": 100, "gst_rate": 18}]})
    assert quote.status_code == 201
    assert client.get(f"/api/v1/quotations/{quote.json()['id']}/pdf", headers=H(owner)).content.startswith(b"%PDF")


def test_gstr1_sections_for_the_test_invoices(client, owner, world):
    data = report(client, owner)
    mine = [r for r in data["b2b"] if r["customer_name"] == world["b2b"]["name"]]
    assert len(mine) == 1  # both lines are 18%: one row per invoice per rate
    assert mine[0]["taxable"] == 2500 and mine[0]["cgst"] == 225 and mine[0]["sgst"] == 225 and mine[0]["igst"] == 0
    assert mine[0]["customer_gstin"] == world["b2b"]["gstin"] and mine[0]["invoice_no"] == world["inv1"]["invoice_no"]
    assert not any(r["customer_name"] == world["b2c"]["name"] for r in data["b2b"])
    assert data["b2cs"] and data["totals"]["taxable"] >= 2800
    assert data["documents"]["net_issued"] >= 2
    hsn = {r["hsn_code"]: r for r in data["hsn"] if r["hsn_code"] in (world["hsn"], "998719", "")}
    assert hsn[world["hsn"]]["quantity"] == 2 and hsn[world["hsn"]]["taxable"] == 2000 and hsn[world["hsn"]]["unit"] == "PCS"
    assert hsn[world["hsn"]]["total_value"] == 2360 and not hsn[world["hsn"]]["missing_hsn"]
    assert hsn["998719"]["taxable"] >= 500
    assert hsn[""]["missing_hsn"] is True and data["warnings"]["lines_without_hsn"] >= 1


def test_gstr1_totals_match_the_existing_gst_summary(client, owner, world):
    data = report(client, owner)
    old = client.get("/api/v1/reports/gst-summary", headers=H(owner), params={"from": TODAY, "to": TODAY}).json()
    for key in ("taxable", "cgst", "sgst", "igst"):
        # gst-summary is net of credit notes (v1.38.0), so it matches the report's net_totals
        assert data["net_totals"][key] == pytest.approx(float(old[f"total_{'taxable' if key == 'taxable' else key}"]), abs=0.01)


def test_cancelled_invoices_leave_the_sections_but_count_in_documents(client, owner, world):
    before = report(client, owner)
    extra = client.post("/api/v1/invoices", headers=H(owner), json={
        "customer_id": world["b2c"]["id"], "allow_negative_stock": True,
        "items": [{"product_id": world["p2"]["id"], "quantity": 1, "unit_price": 100, "gst_rate": 18}]}).json()
    mid = report(client, owner)
    assert mid["documents"]["total_issued"] == before["documents"]["total_issued"] + 1
    assert client.post(f"/api/v1/invoices/{extra['id']}/cancel", headers=H(owner), json={"reason": "test"}).status_code == 200
    after = report(client, owner)
    assert after["totals"]["taxable"] == before["totals"]["taxable"]
    assert after["documents"]["cancelled"] == before["documents"]["cancelled"] + 1
    assert after["documents"]["net_issued"] == before["documents"]["net_issued"]


def test_csv_downloads(client, owner, world):
    q = {"from": TODAY, "to": TODAY}
    r = client.get("/api/v1/reports/gstr1/csv", headers=H(owner), params={**q, "section": "b2b"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and r.headers["content-disposition"].endswith('-b2b.csv"')
    text = r.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("GSTIN/UIN of Recipient") and world["b2b"]["name"] in text
    small = client.get("/api/v1/reports/gstr1/csv", headers=H(owner), params={**q, "section": "hsn"}).content.decode("utf-8-sig")
    assert world["hsn"] in small and "YES" in small
    z = client.get("/api/v1/reports/gstr1/csv", headers=H(owner), params=q)
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert sorted(n.rsplit("-", 1)[1] for n in names) == ["b2b.csv", "b2cl.csv", "b2cs.csv", "cdn.csv", "docs.csv", "hsn.csv"]
    # a customer called "=Walk-in ..." can't turn into a formula in the downloaded sheet
    register = zipfile.ZipFile(io.BytesIO(z.content)).read(next(n for n in names if n.endswith("b2cl.csv")))
    assert b",=Walk-in" not in register


def test_the_walk_in_name_is_escaped_where_it_appears(client, owner, world):
    # B2C small sales are summed by state and rate, so no names there; check the B2B sheet with a registered "=..." buyer
    r = client.post("/api/v1/customers", headers=H(owner), json={"name": f"=cmd {world['tag']}", "gstin": f"{world['b2b']['state_code']}ABCDE1234F1Z5",
                                                                   "state_code": world["b2b"]["state_code"]})
    assert r.status_code == 201
    inv = client.post("/api/v1/invoices", headers=H(owner), json={
        "customer_id": r.json()["id"], "allow_negative_stock": True,
        "items": [{"product_id": world["p2"]["id"], "quantity": 1, "unit_price": 100, "gst_rate": 18}]})
    assert inv.status_code == 201
    text = client.get("/api/v1/reports/gstr1/csv", headers=H(owner), params={"from": TODAY, "to": TODAY, "section": "b2b"}).content.decode("utf-8-sig")
    assert "'=cmd" in text and ",=cmd" not in text


def test_bad_requests(client, owner):
    g = lambda path, **q: client.get(f"/api/v1/reports/{path}", headers=H(owner), params=q).status_code  # noqa: E731
    for path in ("gstr1", "gstr1/csv", "gstr3b", "purchase-register/csv"):
        assert g(path) == 400
        assert g(path, **{"from": "2026-10-10", "to": "2026-10-01"}) == 400
        assert g(path, **{"from": "2020-01-01", "to": "2026-10-01"}) == 400  # longer than the allowed period
        assert g(path, **{"from": "nope", "to": "2026-10-01"}) == 400
    assert g("gstr1/csv", **{"from": TODAY, "to": TODAY, "section": "everything"}) == 400


def test_gstr3b_nets_output_tax_against_credit(client, owner, world):
    r = client.get("/api/v1/reports/gstr3b", headers=H(owner), params={"from": TODAY, "to": TODAY})
    assert r.status_code == 200, r.text
    d = r.json()
    old = client.get("/api/v1/reports/gst-summary", headers=H(owner), params={"from": TODAY, "to": TODAY}).json()
    assert d["output_tax"]["cgst"] == pytest.approx(float(old["total_cgst"]), abs=0.01)
    for head in ("igst", "cgst", "sgst"):
        net = d["output_tax"][head] - d["input_tax_credit"][head]
        assert d["net_payable"][head] == pytest.approx(max(net, 0), abs=0.01)
        assert d["excess_credit"][head] == pytest.approx(max(-net, 0), abs=0.01)
    assert d["total_payable"] == pytest.approx(sum(d["net_payable"].values()), abs=0.01)


def test_purchase_register_csv(client, owner, world):
    sup = client.post("/api/v1/suppliers", headers=H(owner), json={"name": f"Wholesaler {world['tag']}", "gstin": "27ABCDE1234F1Z5", "state_code": "27"})
    assert sup.status_code == 201, sup.text
    p = client.post("/api/v1/purchases", headers=H(owner), json={
        "supplier_id": sup.json()["id"], "supplier_invoice_no": f"S-{world['tag']}",
        "items": [{"product_id": world["p2"]["id"], "quantity": 10, "unit_price": 50, "gst_rate": 18}]})
    assert p.status_code == 201, p.text
    r = client.get("/api/v1/reports/purchase-register/csv", headers=H(owner), params={"from": TODAY, "to": TODAY})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    row = next(line for line in r.content.decode("utf-8-sig").splitlines() if f"S-{world['tag']}" in line)
    assert "27ABCDE1234F1Z5" in row and "27-Maharashtra" in row
    # inter-state purchase: 500 taxable, 90 tax, all of it IGST
    cells = row.split(",")
    assert cells[-5:-1] == ["500.00", "90.00", "0.00", "0.00"]


@pytest.mark.parametrize("role", ["STAFF", "TECHNICIAN"])
def test_only_the_owner_can_use_the_gst_reports(client, owner, role):
    phone = f"96{str(time.time_ns())[-8:]}"
    staff = client.post("/api/v1/auth/users", headers=H(owner), json={"name": f"GSTR {role}", "phone": phone, "role": role}).json()
    token = first_login(client, staff, new_pin="482916")
    for path in ("gstr1", "gstr1/csv", "gstr3b", "purchase-register/csv"):
        r = client.get(f"/api/v1/reports/{path}", headers=H(token), params={"from": TODAY, "to": TODAY})
        assert r.status_code == 403, path
        assert client.get(f"/api/v1/reports/{path}", params={"from": TODAY, "to": TODAY}).status_code in (401, 403)
