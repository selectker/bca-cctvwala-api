"""Business-logic guards: line-item limits, Decimal money, payment limits, stock check, input validation.

The first half needs no database. The second half (test_business_rules_end_to_end) runs against the same
empty PostgreSQL as test_api.py and signs in as the owner that test creates, so keep the file order
(test_api.py runs first).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.billing import LineItemInput, PaymentRequest, PurchaseItemLine
from app.schemas.parties import CreateCustomerRequest, CreateSupplierRequest
from app.schemas.products import CreateProductRequest
from app.utils.dates import today_local
from app.utils.gst import AmountTooLarge, compute_invoice_totals, compute_line_gst, round2


# ---------------- pure unit tests ----------------
def _line(**kw):
    return LineItemInput(**{"description": "x", "quantity": 1, "unit_price": 100, "gst_rate": 18, **kw})


@pytest.mark.parametrize("bad", [
    {"quantity": 0}, {"quantity": -1}, {"quantity": 100001}, {"quantity": 1.234},
    {"unit_price": -5}, {"unit_price": 10_000_001}, {"unit_price": 1.999},
    {"gst_rate": -1}, {"gst_rate": 41}, {"gst_rate": 18.123},
    {"quantity": 100000, "unit_price": 9_999_999},  # line value over the cap
])
def test_line_item_limits_rejected(bad):
    with pytest.raises(ValidationError):
        _line(**bad)
    with pytest.raises(ValidationError):
        PurchaseItemLine(product_id="p", **{"quantity": 1, "unit_price": 100, "gst_rate": 18, **bad})


def test_line_item_limits_accepted_edges():
    assert _line(unit_price=0).unit_price == 0  # free item is fine
    assert _line(gst_rate=0).gst_rate == 0
    assert _line(gst_rate=40).gst_rate == 40
    assert _line(quantity="2.50", unit_price="19.99").quantity == Decimal("2.50")


def test_payment_request_limits():
    for bad in (0, -10, 10 ** 11, 1.005):
        with pytest.raises(ValidationError):
            PaymentRequest(amount=bad)
    PaymentRequest(amount=100, payment_date=today_local())
    with pytest.raises(ValidationError):
        PaymentRequest(amount=100, payment_date=today_local() + timedelta(days=1))


def test_money_is_decimal_and_rounds_half_up():
    assert round2(Decimal("2.675")) == Decimal("2.68")  # round(2.675, 2) == 2.67 with floats
    assert round2(1.005) == Decimal("1.01")
    gst = compute_line_gst(Decimal("3"), Decimal("33.33"), Decimal("18"), "29", "29")
    assert (gst.taxable_value, gst.cgst, gst.sgst, gst.igst, gst.line_total) == (
        Decimal("99.99"), Decimal("9.00"), Decimal("9.00"), Decimal("0.00"), Decimal("117.99"))
    assert all(isinstance(v, Decimal) for v in (gst.taxable_value, gst.cgst, gst.sgst, gst.igst, gst.line_total))
    inter = compute_line_gst(Decimal("3"), Decimal("33.33"), Decimal("18"), "27", "29")
    assert inter.igst == Decimal("18.00") and inter.cgst == inter.sgst == Decimal("0.00")
    totals = compute_invoice_totals([gst])
    assert totals.total_amount == Decimal("118.00") and totals.round_off == Decimal("0.01")


def test_no_float_drift_across_many_lines():
    # 1000 lines of 0.10 must add up to exactly 100.00 (float: 99.99999999999859). GST is rounded per
    # line (0.009 -> 0.01 each way), so CGST and SGST are 10.00 each.
    lines = [compute_line_gst(1, "0.10", 18, "29", "29") for _ in range(1000)]
    totals = compute_invoice_totals(lines)
    assert totals.subtotal == Decimal("100.00")
    assert totals.total_cgst == totals.total_sgst == Decimal("10.00")
    assert totals.total_amount == Decimal("120.00") and totals.round_off == Decimal("0.00")


def test_half_rupee_rounds_up():
    line = compute_line_gst(1, "100.50", 0, "29", "29")
    totals = compute_invoice_totals([line])
    assert totals.total_amount == Decimal("101.00") and totals.round_off == Decimal("0.50")


def test_document_total_overflow_is_refused():
    big = compute_line_gst(100000, "10000000", 40, "29", "29")
    with pytest.raises(AmountTooLarge):
        compute_invoice_totals([big])


def test_party_validation():
    ok = CreateCustomerRequest(name="  Asha  ", gstin=" 29abcde1234f1z5 ", phone="+91 98765-43210",
                               email=" a@b.co ", state_code="29")
    assert (ok.name, ok.gstin, ok.phone, ok.email) == ("Asha", "29ABCDE1234F1Z5", "+919876543210", "a@b.co")
    blank = CreateCustomerRequest(name="A", gstin="", phone=" ", email="", state_code="")
    assert blank.gstin is None and blank.phone is None and blank.email is None and blank.state_code is None
    for bad in (
        {"name": "   "}, {"name": "x" * 151},
        {"name": "A", "gstin": "29ABCDE1234F1Z"}, {"name": "A", "gstin": "99ABCDE1234F1Z5X"},
        {"name": "A", "gstin": "00ABCDE1234F1Z5"}, {"name": "A", "gstin": "29ABCDE1234F1Y5"},
        {"name": "A", "phone": "12345"}, {"name": "A", "phone": "abcdefghij"}, {"name": "A", "phone": "9" * 16},
        {"name": "A", "email": "nope"}, {"name": "A", "email": "a@b"}, {"name": "A", "email": "x" * 150 + "@b.co"},
        {"name": "A", "state_code": "99999"}, {"name": "A", "state_code": "00"}, {"name": "A", "state_code": "ab"},
        {"name": "A", "customer_type": "VIP"}, {"name": "A", "opening_balance": 10 ** 12},
    ):
        with pytest.raises(ValidationError):
            CreateCustomerRequest(**bad)
        if "customer_type" not in bad:
            with pytest.raises(ValidationError):
                CreateSupplierRequest(**bad)


def test_product_validation():
    p = CreateProductRequest(name=" Cable ", hsn_code="8544", gst_rate=18, selling_price="99.50", reorder_level=3)
    assert p.name == "Cable" and p.selling_price == Decimal("99.50")
    for bad in ({"gst_rate": 41}, {"gst_rate": -1}, {"purchase_price": -1}, {"selling_price": -0.01},
                {"reorder_level": -1}, {"reorder_level": 2.5}, {"hsn_code": "ABCD"}, {"name": ""}):
        with pytest.raises(ValidationError):
            CreateProductRequest(**{"name": "Cable", **bad})


# ---------------- end to end ----------------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


def test_business_rules_end_to_end(client):
    ot = ok(client.post("/api/v1/auth/login", json={"phone": "9000000001", "password": "ownerpass1"}))["token"]
    h = H(ot)

    cust = ok(client.post("/api/v1/customers", headers=h, json={
        "name": "Rules Cust", "phone": "98765 43210", "gstin": "29abcde1234f1z5", "state_code": "29"}), 201)
    assert cust["phone"] == "9876543210" and cust["gstin"] == "29ABCDE1234F1Z5"
    sup = ok(client.post("/api/v1/suppliers", headers=h, json={"name": "Rules Sup"}), 201)

    # -- input validation reaches the API as 422, not a 500 from the database --
    assert client.post("/api/v1/customers", headers=h, json={"name": "X", "gstin": "BAD"}).status_code == 422
    assert client.post("/api/v1/customers", headers=h, json={"name": "X", "phone": "1"}).status_code == 422
    assert client.post("/api/v1/customers", headers=h, json={"name": "X" * 200}).status_code == 422
    assert client.post("/api/v1/customers", headers=h, json={"name": "X", "state_code": "77"}).status_code == 422
    assert client.put(f"/api/v1/customers/{cust['id']}", headers=h, json={"name": "X", "email": "nope"}).status_code == 422
    assert client.put("/api/v1/store-profile", headers=h, json={"store_name": "Z", "gstin": "123"}).status_code == 422
    assert client.post("/api/v1/suppliers", headers=h, json={"name": "X", "gstin": "29ABCDE1234F1Z"}).status_code == 422
    assert client.post("/api/v1/products", headers=h, json={"name": "P", "gst_rate": 99}).status_code == 422

    cable = ok(client.post("/api/v1/products", headers=h, json={
        "name": "Rules Cable", "gst_rate": 18, "unit": "MTR", "selling_price": 10}), 201)
    pid = cable["id"]

    # -- line-item limits on invoices, quotations and purchases --
    def line(**kw):
        return {"product_id": pid, "quantity": 1, "unit_price": 100, "gst_rate": 18, **kw}

    for bad in ({"unit_price": -100}, {"quantity": -1}, {"quantity": 0}, {"gst_rate": 500}, {"gst_rate": -18}):
        assert client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": [line(**bad)]}).status_code == 422
        assert client.post("/api/v1/quotations", headers=h, json={"customer_id": cust["id"], "items": [line(**bad)]}).status_code == 422
        assert client.post("/api/v1/purchases", headers=h, json={"supplier_id": sup["id"], "items": [line(**bad)]}).status_code == 422
    assert client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": []}).status_code == 400

    # -- purchase 10 units: GST computed exactly --
    pur = ok(client.post("/api/v1/purchases", headers=h, json={
        "supplier_id": sup["id"], "items": [line(quantity=10, unit_price="33.33")]}), 201)
    assert Decimal(str(pur["subtotal"])) == Decimal("333.30") and Decimal(str(pur["total_amount"])) == Decimal("393.29")

    # -- payments on a purchase: capped at the balance, never dated in the future --
    purl = f"/api/v1/purchases/{pur['id']}/payments"
    assert client.post(purl, headers=h, json={"amount": 393.30}).status_code == 400
    tomorrow = (today_local() + timedelta(days=1)).isoformat()
    assert client.post(purl, headers=h, json={"amount": 10, "payment_date": tomorrow}).status_code == 422
    ok(client.post(purl, headers=h, json={"amount": 393.00}))
    assert client.post(purl, headers=h, json={"amount": 0.30}).status_code == 400  # only 0.29 left
    paid = ok(client.post(purl, headers=h, json={"amount": 0.29}))
    assert paid["payment_status"] == "PAID"
    assert client.post(purl, headers=h, json={"amount": 1}).status_code == 400  # fully paid

    # -- stock check: 10 in stock --
    inv = ok(client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": [line(quantity=10)]}), 201)
    assert Decimal(str(inv["total_amount"])) == Decimal("1180.00") and inv["warnings"] == []

    def stock():
        return Decimal(str(ok(client.get(f"/api/v1/products/{pid}", headers=h))["current_stock"]))

    assert stock() == 0
    refused = client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": [line()]})
    assert refused.status_code == 409 and "Rules Cable" in refused.json()["error"] and stock() == 0
    # two lines of the same product are added together
    ok(client.post("/api/v1/purchases", headers=h, json={"supplier_id": sup["id"], "items": [line(quantity=3)]}), 201)
    two = client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": [line(quantity=2), line(quantity=2)]})
    assert two.status_code == 409 and stock() == 3
    assert client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": [
        {"product_id": "not-a-uuid", "quantity": 1, "unit_price": 1, "gst_rate": 0}]}).status_code == 400
    assert client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": [
        {"product_id": "00000000-0000-0000-0000-000000000000", "quantity": 1, "unit_price": 1, "gst_rate": 0}]}).status_code == 404
    # a refused sale leaves nothing behind
    assert len(ok(client.get("/api/v1/invoices", headers=h, params={"search": "Rules Cust"}))) == 1
    # selling anyway is explicit, goes negative, and says so
    forced = ok(client.post("/api/v1/invoices", headers=h, json={
        "customer_id": cust["id"], "allow_negative_stock": True, "items": [line(quantity=5)]}), 201)
    assert len(forced["warnings"]) == 1 and "Rules Cable" in forced["warnings"][0] and stock() == -2
    # description-only lines (service work) have no stock to check
    ok(client.post("/api/v1/invoices", headers=h, json={"customer_id": cust["id"], "items": [
        {"description": "Visit", "quantity": 1, "unit_price": 500, "gst_rate": 18}]}), 201)

    # -- stock adjustments can't push stock below zero either --
    assert client.post(f"/api/v1/products/{pid}/adjust-stock", headers=h, json={"quantity": -50}).status_code == 409
    assert client.post(f"/api/v1/products/{pid}/adjust-stock", headers=h, json={"quantity": 0}).status_code == 400
    assert client.post("/api/v1/products/00000000-0000-0000-0000-000000000000/adjust-stock", headers=h,
                       json={"quantity": 1}).status_code == 404
    ok(client.post(f"/api/v1/products/{pid}/adjust-stock", headers=h, json={"quantity": 12, "note": "found"}), 201)
    assert stock() == 10

    # -- payments on an invoice: capped at the balance, never future-dated --
    iurl = f"/api/v1/invoices/{inv['id']}/payments"  # total 1180.00
    assert client.post(iurl, headers=h, json={"amount": 1180.01}).status_code == 400
    assert client.post(iurl, headers=h, json={"amount": -5}).status_code == 422
    assert client.post(iurl, headers=h, json={"amount": 100, "payment_date": tomorrow}).status_code == 422
    part = ok(client.post(iurl, headers=h, json={"amount": 1000, "payment_date": today_local().isoformat()}))
    assert part["payment_status"] == "PARTIAL"
    assert client.post(iurl, headers=h, json={"amount": 180.01}).status_code == 400
    done = ok(client.post(iurl, headers=h, json={"amount": 180}))
    assert done["payment_status"] == "PAID" and Decimal(str(done["paid_amount"])) == Decimal("1180.00")
    assert client.post(iurl, headers=h, json={"amount": 1}).status_code == 400

    # -- quotation -> invoice conversion goes through the same stock check --
    q = ok(client.post("/api/v1/quotations", headers=h, json={"customer_id": cust["id"], "items": [line(quantity=11)]}), 201)
    assert client.post(f"/api/v1/invoices/from-quotation/{q['id']}", headers=h).status_code == 409
    conv = ok(client.post(f"/api/v1/invoices/from-quotation/{q['id']}", headers=h, json={"allow_negative_stock": True}), 201)
    assert len(conv["warnings"]) == 1


# ---------------- line discount (no database) ----------------
def test_discount_reduces_taxable_value_before_gst():
    gst = compute_line_gst(2, Decimal("1000"), 18, "27", "27", Decimal("10"))  # gross 2000, 10% off
    assert gst.discount_amount == Decimal("200.00")
    assert gst.taxable_value == Decimal("1800.00")
    assert gst.cgst == gst.sgst == Decimal("162.00")  # 9% of 1800, not of 2000
    assert gst.line_total == Decimal("2124.00")


def test_discount_interstate_uses_igst_on_discounted_value():
    gst = compute_line_gst(1, Decimal("999.99"), 18, "29", "27", Decimal("12.5"))
    assert gst.is_interstate and gst.cgst == gst.sgst == 0
    assert gst.discount_amount == Decimal("125.00")  # 124.99875 rounds half-up
    assert gst.taxable_value == Decimal("874.99")
    assert gst.igst == round2(Decimal("874.99") * 18 / 100)


def test_no_discount_is_unchanged_and_totals_sum_discounts():
    assert compute_line_gst(3, 100, 18, "27", "27").discount_amount == 0
    lines = [compute_line_gst(1, 100, 18, "27", "27", 10), compute_line_gst(1, 200, 18, "27", "27", 50)]
    totals = compute_invoice_totals(lines)
    assert totals.total_discount == Decimal("110.00")
    assert totals.subtotal == Decimal("190.00")  # taxable value, already after discount


def test_full_discount_is_allowed_and_bad_percentages_rejected():
    assert compute_line_gst(1, 100, 18, "27", "27", 100).line_total == 0
    assert _line().discount_percent == 0
    for bad in (-1, 100.01, 10.123):
        with pytest.raises(ValidationError):
            _line(discount_percent=bad)


# ---------- invoice-level discount ----------
def test_invoice_discount_percent_is_spread_before_gst():
    from app.utils.gst import compute_lines_with_invoice_discount
    specs = [(1, 1000, 18, 0), (2, 500, 12, 0)]  # 1000 @18% + 1000 @12%
    lines, disc = compute_lines_with_invoice_discount(specs, "29", "29", 10)
    assert disc == Decimal("200.00")
    assert [l.taxable_value for l in lines] == [Decimal("900.00"), Decimal("900.00")]
    assert lines[0].cgst == Decimal("81.00") and lines[1].cgst == Decimal("54.00")
    assert sum(l.discount_amount for l in lines) == disc
    assert compute_invoice_totals(lines).total_discount == disc


def test_invoice_discount_flat_amount_shares_add_up_exactly():
    from app.utils.gst import compute_lines_with_invoice_discount
    specs = [(1, "33.33", 18, 0), (1, "33.33", 18, 0), (1, "33.34", 18, 0)]
    lines, disc = compute_lines_with_invoice_discount(specs, "27", "29", 0, "10.00")
    assert disc == Decimal("10.00")
    assert sum(l.discount_amount for l in lines) == Decimal("10.00")
    assert all(l.igst > 0 and l.cgst == 0 for l in lines)


def test_invoice_discount_stacks_on_line_discount_and_is_capped():
    from app.utils.gst import compute_lines_with_invoice_discount
    lines, disc = compute_lines_with_invoice_discount([(1, 1000, 18, 10)], "29", "29", 10)
    assert disc == Decimal("90.00") and lines[0].taxable_value == Decimal("810.00")
    with pytest.raises(ValueError):
        compute_lines_with_invoice_discount([(1, 100, 18, 0)], "29", "29", 0, "100.01")
    lines, disc = compute_lines_with_invoice_discount([(1, 100, 18, 0)], "29", "29", 100)
    assert lines[0].taxable_value == 0 and disc == Decimal("100.00")


def test_invoice_discount_none_changes_nothing():
    from app.utils.gst import compute_lines_with_invoice_discount
    lines, disc = compute_lines_with_invoice_discount([(3, 100, 18, 0)], "29", "29")
    assert disc == 0 and lines[0].taxable_value == Decimal("300.00")
