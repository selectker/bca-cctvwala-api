"""v1.18.0: expenses (CRUD, filters, summary, audit, permissions) and the profit-loss report.

Same setup as test_malformed_input.py: a real PostgreSQL; the admin / owner are bootstrapped when missing.
The profit-loss test compares the report before and after, so other tests' invoices don't matter.
"""
import os
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import first_login

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
TODAY = date.today().isoformat()


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


def new_staff(client, owner, label, role="STAFF"):
    phone = f"96{str(time.time_ns())[-8:]}"
    r = client.post("/api/v1/auth/users", headers=H(owner), json={"name": f"EX {label}", "phone": phone, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def make(client, owner, **fields):
    body = {"category": "Rent", "amount": 1000, "expense_date": "2018-04-10", **fields}
    r = client.post("/api/v1/expenses", headers=H(owner), json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---- who may use it ----
@pytest.mark.parametrize("role", ["STAFF", "TECHNICIAN"])
def test_only_the_owner_can_use_expenses(client, owner, role):
    staff = new_staff(client, owner, "expenses", role)
    staff_token = first_login(client, staff, new_pin="482916")
    for method, path in [("GET", "/expenses"), ("POST", "/expenses"), ("GET", "/expenses/summary?from=2018-01-01&to=2018-12-31"),
                         ("GET", "/expenses/categories"), ("GET", "/reports/profit-loss?from=2018-01-01&to=2018-12-31")]:
        r = client.request(method, "/api/v1" + path, headers=H(staff_token), json={"category": "Rent", "amount": 5})
        assert r.status_code == 403, f"{method} {path}: {r.status_code}"
        assert client.request(method, "/api/v1" + path).status_code in (401, 403)


# ---- create ----
def test_create_returns_the_expense_with_defaults(client, owner):
    r = client.post("/api/v1/expenses", headers=H(owner), json={"category": "Travel & fuel", "amount": "1250.50"})
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["expense_date"] == TODAY and e["payment_mode"] == "CASH" and float(e["amount"]) == 1250.5
    assert e["paid_to"] is None and e["created_by_name"] == "Owner"


def test_categories_keep_one_spelling(client, owner):
    assert make(client, owner, category="rent")["category"] == "Rent"                      # a suggested name, any case
    assert make(client, owner, category="  salary   &  wages ")["category"] == "Salary & wages"  # extra spaces
    first = make(client, owner, category="Tea & snacks")["category"]
    assert first == "Tea & snacks"
    assert make(client, owner, category="tea & SNACKS")["category"] == "Tea & snacks"      # a name used before
    names = client.get("/api/v1/expenses/categories", headers=H(owner)).json()
    assert "Rent" in names and "Tea & snacks" in names and names == sorted(names, key=str.lower)
    assert len([n for n in names if n.lower() == "tea & snacks"]) == 1


def test_blank_optional_text_is_stored_as_nothing(client, owner):
    e = make(client, owner, paid_to="   ", reference_no="", notes="  ")
    assert e["paid_to"] is None and e["reference_no"] is None and e["notes"] is None


@pytest.mark.parametrize("bad", [
    {"amount": 0}, {"amount": -5}, {"amount": "12.345"}, {"amount": 10 ** 12}, {"amount": "abc"},
    {"category": ""}, {"category": "   "}, {"category": "x" * 61}, {"payment_mode": "BARTER"},
    {"expense_date": (date.today() + timedelta(days=1)).isoformat()}, {"expense_date": "2018-13-45"},
    {"paid_to": "x" * 121}, {"reference_no": "x" * 51}, {"notes": "x" * 2001},
])
def test_bad_expenses_are_refused(client, owner, bad):
    r = client.post("/api/v1/expenses", headers=H(owner), json={"category": "Rent", "amount": 100, **bad})
    assert r.status_code == 422, f"{bad}: {r.status_code} {r.text}"


# ---- list / filters / totals ----
def test_list_filters_and_totals(client, owner):
    a = make(client, owner, category="Electricity & water", amount="300.25", expense_date="2017-02-10", paid_to="State Board")
    make(client, owner, category="Electricity & water", amount="200", expense_date="2017-02-20", notes="100% of the bill")
    make(client, owner, category="Insurance", amount="500", expense_date="2017-03-05")
    r = client.get("/api/v1/expenses", headers=H(owner), params={"from": "2017-02-01", "to": "2017-03-31"})
    assert r.status_code == 200 and len(r.json()) == 3
    assert r.headers["X-Total-Count"] == "3" and float(r.headers["X-Total-Amount"]) == 1000.25
    dates = [e["expense_date"] for e in r.json()]
    assert dates == sorted(dates, reverse=True)                                              # newest first

    only = client.get("/api/v1/expenses", headers=H(owner), params={"from": "2017-02-01", "to": "2017-03-31", "category": "electricity & WATER"})
    assert only.headers["X-Total-Count"] == "2" and float(only.headers["X-Total-Amount"]) == 500.25

    page = client.get("/api/v1/expenses", headers=H(owner), params={"from": "2017-02-01", "to": "2017-03-31", "limit": 1})
    assert len(page.json()) == 1 and page.headers["X-Total-Count"] == "3" and float(page.headers["X-Total-Amount"]) == 1000.25

    by_name = client.get("/api/v1/expenses", headers=H(owner), params={"search": "state board"}).json()
    assert [e["id"] for e in by_name] == [a["id"]]
    # % is a literal character in a search, not "anything"
    pct = client.get("/api/v1/expenses", headers=H(owner), params={"search": "100%", "from": "2017-01-01", "to": "2017-12-31"})
    assert pct.headers["X-Total-Count"] == "1"
    none = client.get("/api/v1/expenses", headers=H(owner), params={"from": "1999-01-01", "to": "1999-12-31"})
    assert none.json() == [] and none.headers["X-Total-Count"] == "0" and float(none.headers["X-Total-Amount"]) == 0


def test_summary_by_category_and_month(client, owner):
    make(client, owner, category="Marketing", amount="400", expense_date="2016-05-02")
    make(client, owner, category="marketing", amount="100.50", expense_date="2016-05-20")
    make(client, owner, category="Rent", amount="1000", expense_date="2016-06-01")
    s = client.get("/api/v1/expenses/summary", headers=H(owner), params={"from": "2016-05-01", "to": "2016-06-30"}).json()
    assert s["count"] == 3 and float(s["total"]) == 1500.5
    assert [(c["category"], float(c["total"]), c["count"]) for c in s["by_category"]] == [("Rent", 1000.0, 1), ("Marketing", 500.5, 2)]
    assert [(m["month"], float(m["total"])) for m in s["by_month"]] == [("2016-05", 500.5), ("2016-06", 1000.0)]
    assert client.get("/api/v1/expenses/summary", headers=H(owner)).status_code == 400


# ---- change / delete, and the audit trail ----
def audit_rows(client, owner, expense_id):
    r = client.get("/api/v1/audit-logs", headers=H(owner), params={"entity_type": "EXPENSE", "limit": 100})
    assert r.status_code == 200, r.text
    return [row for row in r.json() if row["entity_id"] == expense_id]


def test_update_and_delete_are_audited_with_the_amounts(client, owner):
    e = make(client, owner, category="Repairs & maintenance", amount="800", expense_date="2015-01-15")
    r = client.put(f"/api/v1/expenses/{e['id']}", headers=H(owner), json={"category": "Repairs & maintenance", "amount": "950.75", "expense_date": "2015-01-16", "notes": "AC service"})
    assert r.status_code == 200 and float(r.json()["amount"]) == 950.75 and r.json()["updated_at"]
    assert client.get(f"/api/v1/expenses/{e['id']}", headers=H(owner)).json()["notes"] == "AC service"

    assert client.delete(f"/api/v1/expenses/{e['id']}", headers=H(owner)).status_code == 204
    assert client.get(f"/api/v1/expenses/{e['id']}", headers=H(owner)).status_code == 404
    assert client.delete(f"/api/v1/expenses/{e['id']}", headers=H(owner)).status_code == 404
    assert client.put(f"/api/v1/expenses/{e['id']}", headers=H(owner), json={"category": "Rent", "amount": 1}).status_code == 404

    rows = audit_rows(client, owner, e["id"])
    actions = sorted(r["action"] for r in rows)
    assert actions == ["CREATE", "DELETE", "UPDATE"], actions            # one row each, no duplicates
    # the list shows summaries; the amounts are in each row's details (the single-row view)
    by = {r["action"]: client.get(f"/api/v1/audit-logs/{r['id']}", headers=H(owner)).json() for r in rows}
    assert "₹800.00 to ₹950.75" in by["UPDATE"]["summary"]
    assert by["UPDATE"]["details"]["changes"]["amount"] == {"from": "800.00", "to": "950.75"}
    assert "₹950.75" in by["DELETE"]["summary"] and "2015-01-16" in by["DELETE"]["summary"]
    assert by["DELETE"]["details"]["amount"] == "950.75" and by["DELETE"]["details"]["notes"] == "AC service"


# ---- profit & loss ----
def pnl(client, owner):
    r = client.get("/api/v1/reports/profit-loss", headers=H(owner), params={"from": TODAY, "to": TODAY})
    assert r.status_code == 200, r.text
    return r.json()


def test_profit_loss_math(client, owner):
    assert client.get("/api/v1/reports/profit-loss", headers=H(owner)).status_code == 400
    before = pnl(client, owner)

    cust = client.post("/api/v1/customers", headers=H(owner), json={"name": "P&L Customer", "phone": "9822222222"}).json()["id"]
    costed = client.post("/api/v1/products", headers=H(owner), json={
        "name": "P&L Costed", "unit": "PCS", "gst_rate": 18, "selling_price": 100, "purchase_price": 60, "reorder_level": 0}).json()["id"]
    uncosted = client.post("/api/v1/products", headers=H(owner), json={
        "name": "P&L Uncosted", "unit": "PCS", "gst_rate": 18, "selling_price": 40, "purchase_price": 0, "reorder_level": 0}).json()["id"]
    for pid in (costed, uncosted):
        assert client.post(f"/api/v1/products/{pid}/adjust-stock", headers=H(owner), json={"quantity": 20, "note": "seed"}).status_code == 201

    def line(**kw):
        return {"description": "x", "quantity": 1, "unit_price": 100, "gst_rate": 18, **kw}

    inv = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": cust, "items": [
        line(product_id=costed, quantity=2, unit_price=100),            # taxable 200, cost 2 x 60 = 120
        line(description="Installation", unit_price=50),                # a service line: taxable 50, no cost
        line(product_id=uncosted, quantity=1, unit_price=40),           # taxable 40, cost unknown (0)
    ]})
    assert inv.status_code == 201, inv.text
    # a second invoice that is cancelled must not count
    gone = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": cust, "items": [line(product_id=costed, quantity=3)]}).json()
    assert client.post(f"/api/v1/invoices/{gone['id']}/cancel", headers=H(owner), json={"reason": "test", "refund_payments": True}).status_code == 200
    spent = make(client, owner, category="Rent", amount="30", expense_date=TODAY)

    after = pnl(client, owner)
    d = lambda key: round(after[key] - before[key], 2)
    assert d("invoice_count") == 1
    assert d("sales_taxable") == 290.0 and d("cost_of_goods") == 120.0 and d("gross_profit") == 170.0
    assert d("expenses_total") == 30.0 and d("expense_count") == 1 and d("net_profit") == 140.0
    assert d("lines_without_cost") == 1
    assert after["cost_is_estimate"] is True and after["from"] == TODAY
    assert any(c["category"] == "Rent" for c in after["expenses_by_category"])
    assert after["gross_margin_percent"] == round(after["gross_profit"] * 100 / after["sales_taxable"], 1)

    # deleting the expense takes it out of the profit again
    assert client.delete(f"/api/v1/expenses/{spent['id']}", headers=H(owner)).status_code == 204
    assert round(pnl(client, owner)["net_profit"] - before["net_profit"], 2) == 170.0


def test_profit_loss_of_an_empty_period_is_zero_not_an_error(client, owner):
    r = client.get("/api/v1/reports/profit-loss", headers=H(owner), params={"from": "1990-01-01", "to": "1990-01-31"}).json()
    assert r["sales_taxable"] == 0 and r["net_profit"] == 0 and r["gross_margin_percent"] == 0 and r["net_margin_percent"] == 0
    assert r["expenses_by_category"] == []
