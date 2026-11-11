"""End-to-end API test against a real PostgreSQL (see tests/README in the main README).

    createdb gst && psql gst -f db/schema.sql   # fresh, empty database
    DB_HOST=... DB_NAME=gst ... DB_SSL=false pytest -q tests   # tests/conftest.py supplies a test JWT_SECRET

It runs one long scenario on an EMPTY database, so don't point it at real data.
"""
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import first_login, raw_connect


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


def test_full_scenario(client):
    # ---- version / bootstrap: two separate steps, each can only ever run once ----
    assert ok(client.get("/api/v1/version"))["version"]
    assert ok(client.get("/api/v1/auth/bootstrap-status")) == {"needs_admin_bootstrap": True, "needs_owner_bootstrap": False}
    body = {"name": "Admin", "phone": "9000000000", "password": "adminpass1"}
    for route in ("bootstrap-admin", "bootstrap-owner"):
        no_token = client.post(f"/api/v1/auth/{route}", json=body, headers={"X-Setup-Token": ""})
        wrong = client.post(f"/api/v1/auth/{route}", json=body, headers={"X-Setup-Token": "wrong"})
        assert no_token.status_code == 403 and wrong.status_code == 403
    assert ok(client.get("/api/v1/auth/bootstrap-status"))["needs_admin_bootstrap"] is True  # nothing was created
    assert client.post("/api/v1/auth/bootstrap-admin", json={"name": "A", "phone": "9000000000", "password": "short"}).status_code == 422
    ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", "phone": "9000000000", "password": "adminpass1"}), 201)
    assert ok(client.get("/api/v1/auth/bootstrap-status")) == {"needs_admin_bootstrap": False, "needs_owner_bootstrap": True}
    assert client.post("/api/v1/auth/bootstrap-admin", json={"name": "A2", "phone": "9000000099", "password": "adminpass2"}).status_code == 403
    # owner bootstrap can't happen before the admin exists (checked again on a fresh scenario below);
    # here the admin already exists, so this succeeds
    assert client.post("/api/v1/auth/bootstrap-owner", json={"name": "O", "phone": "9000000001", "password": "short"}).status_code == 422
    ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", "phone": "9000000001", "password": "ownerpass1"}), 201)
    assert ok(client.get("/api/v1/auth/bootstrap-status")) == {"needs_admin_bootstrap": False, "needs_owner_bootstrap": False}
    assert client.post("/api/v1/auth/bootstrap-owner", json={"name": "X", "phone": "9000000002", "password": "ownerpass2"}).status_code == 403
    owner = ok(client.post("/api/v1/auth/login", json={"phone": "9000000001", "password": "ownerpass1"}))
    ot = owner["token"]

    # ---- users: create (PIN, not password) / list / team / update / reset / deactivate ----
    staff = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Sam", "phone": "9000000003", "role": "STAFF"}), 201)
    tech = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Tia", "phone": "9000000004", "role": "TECHNICIAN"}), 201)
    assert len(staff["pin"]) == 6 and staff["must_change_pin"] is True
    users = client.get("/api/v1/auth/users", headers=H(ot))
    assert users.status_code == 200 and users.headers["X-Total-Count"] == "3" and len(users.json()) == 3  # owner + staff + tech; admin never listed
    assert client.post("/api/v1/auth/login", json={"phone": "9000000003", "password": staff["pin"]}).status_code == 401  # staff has no password
    st = ok(client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": staff["pin"]}))
    assert st["must_change_pin"] is True
    st = st["token"]
    assert client.get("/api/v1/auth/users", headers=H(st)).status_code == 403  # owner only
    # the one-time PIN can only be used to change it (and to read the own account)
    blocked = client.get("/api/v1/auth/team", headers=H(st))
    assert blocked.status_code == 403 and blocked.headers["X-Must-Change-Pin"] == "true"
    assert client.get("/api/v1/store-profile", headers=H(st)).status_code == 403
    assert ok(client.get("/api/v1/auth/me", headers=H(st)))["must_change_pin"] is True
    assert client.post("/api/v1/auth/change-pin", headers=H(st), json={"current_pin": staff["pin"], "new_pin": staff["pin"]}).status_code == 400  # must differ
    # owner and platform admin accounts can't be edited through this endpoint
    me = [u for u in users.json() if u["role"] == "OWNER"][0]
    assert client.put(f"/api/v1/auth/users/{me['id']}", headers=H(ot), json={"is_active": False}).status_code == 400
    # self-service PIN change requires the current PIN
    assert client.post("/api/v1/auth/change-pin", headers=H(st), json={"current_pin": "000000", "new_pin": "111111"}).status_code == 401
    time.sleep(1.1)
    changed = ok(client.post("/api/v1/auth/change-pin", headers=H(st), json={"current_pin": staff["pin"], "new_pin": "222222"}))
    assert changed["user"]["must_change_pin"] is False
    assert client.get("/api/v1/auth/team", headers=H(st)).status_code == 401  # old token dead
    st2 = ok(client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": "222222"}))["token"]
    assert client.get("/api/v1/auth/team", headers=H(st2)).status_code == 200
    team = ok(client.get("/api/v1/auth/team", headers=H(st2)))
    assert team[0]["role"] == "TECHNICIAN" and "phone" not in team[0]
    # forgotten PIN: only the owner can recover it, via reset-pin
    time.sleep(1.1)  # token iat has 1-second resolution
    reset = ok(client.post(f"/api/v1/auth/users/{staff['id']}/reset-pin", headers=H(ot)))
    assert len(reset["pin"]) == 6 and reset["must_change_pin"] is True
    assert client.get("/api/v1/auth/team", headers=H(st2)).status_code == 401  # reset also kills old tokens
    st2 = ok(client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": reset["pin"]}))["token"]
    assert client.get("/api/v1/auth/team", headers=H(st2)).status_code == 403  # the reset PIN is one-time too
    st2 = ok(client.post("/api/v1/auth/change-pin", headers=H(st2), json={"current_pin": reset["pin"], "new_pin": "333333"}))["token"]
    assert client.get("/api/v1/auth/team", headers=H(st2)).status_code == 200
    # deactivate -> token dead, login refused
    ok(client.put(f"/api/v1/auth/users/{staff['id']}", headers=H(ot), json={"is_active": False}))
    assert client.get("/api/v1/auth/team", headers=H(st2)).status_code == 401
    assert client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": "333333"}).status_code == 401
    ok(client.put(f"/api/v1/auth/users/{staff['id']}", headers=H(ot), json={"is_active": True}))
    st2 = ok(client.post("/api/v1/auth/login-pin", json={"account_id": staff["id"], "pin": "333333"}))["token"]

    # ---- store profile ----
    assert client.put("/api/v1/store-profile", headers=H(st2), json={"store_name": "Z"}).status_code == 403
    prof = ok(client.put("/api/v1/store-profile", headers=H(ot), json={
        "store_name": "BCA CCTV", "gstin": "29ABCDE1234F1Z5", "address": "MG Road", "state": "Karnataka", "state_code": "29",
        "bank_name": "HDFC", "bank_account_no": "123", "bank_ifsc": "HDFC0001", "invoice_prefix": "bca", "quotation_prefix": "QT"}))
    assert prof["invoice_prefix"] == "BCA" and ok(client.get("/api/v1/store-profile", headers=H(ot)))["bank_name"] == "HDFC"
    staff_view = ok(client.get("/api/v1/store-profile", headers=H(st2)))  # staff see the shop, never its bank account
    assert staff_view["store_name"] and not {"bank_name", "bank_account_no", "bank_ifsc"} & staff_view.keys()
    assert {"bank_name", "bank_account_no", "bank_ifsc"} <= ok(client.get("/api/v1/store-profile", headers=H(ot))).keys()
    assert client.put("/api/v1/store-profile", headers=H(ot), json={"store_name": "Z", "state_code": "abc"}).status_code == 422

    # ---- logo: owner uploads, everyone signed in can see it, invoices / quotations print it ----
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGBA", (800, 400), (200, 30, 30, 255)).save(buf, "PNG")
    png = buf.getvalue()
    assert client.get("/api/v1/store-profile/logo", headers=H(ot)).status_code == 404            # none yet
    assert ok(client.get("/api/v1/store-profile", headers=H(ot)))["has_logo"] is False
    assert client.post("/api/v1/store-profile/logo", headers=H(st2), files={"logo": ("l.png", png, "image/png")}).status_code == 403
    assert client.post("/api/v1/store-profile/logo", headers=H(ot), files={"logo": ("l.png", b"not an image", "image/png")}).status_code == 400
    assert client.post("/api/v1/store-profile/logo", headers=H(ot)).status_code == 422             # no file at all
    with_logo = ok(client.post("/api/v1/store-profile/logo", headers=H(ot), files={"logo": ("l.png", png, "image/png")}))
    assert with_logo["has_logo"] is True and "logo_key" not in with_logo and with_logo["store_name"] == "BCA CCTV"
    shown = client.get("/api/v1/store-profile/logo", headers=H(st2))                               # staff can fetch it too
    assert shown.status_code == 200 and shown.headers["content-type"] == "image/png" and Image.open(BytesIO(shown.content)).size == (600, 300)  # shrunk to MAX_SIDE_PX (600), aspect ratio kept
    assert ok(client.get("/api/v1/store-profile", headers=H(st2)))["has_logo"] is True and "logo_key" not in ok(client.get("/api/v1/store-profile", headers=H(ot)))
    assert ok(client.put("/api/v1/store-profile", headers=H(ot), json={"store_name": "BCA CCTV", "state_code": "29", "invoice_prefix": "BCA", "quotation_prefix": "QT",
                                                                    "bank_name": "HDFC"}))["has_logo"] is True  # editing the details keeps the logo
    assert client.delete("/api/v1/store-profile/logo", headers=H(st2)).status_code == 403
    # the signature image itself is owner-only: staff can't download it (v1.17.2), the owner gets a 404 while none is saved
    assert client.get("/api/v1/store-profile/signature", headers=H(st2)).status_code == 403
    assert client.get("/api/v1/store-profile/signature", headers=H(ot)).status_code == 404
    assert client.get("/api/v1/store-profile/signature").status_code in (401, 403)

    # ---- parties, products ----
    cust = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "Asha", "phone": "9111111111", "state_code": "29", "opening_balance": 500}), 201)
    cust_igst = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "Ravi", "state_code": "27"}), 201)
    sup = ok(client.post("/api/v1/suppliers", headers=H(ot), json={"name": "Hik Dist", "phone": "9222222222", "opening_balance": 100}), 201)
    cat = ok(client.post("/api/v1/products/meta/categories", headers=H(ot), json={"name": "Camera"}), 201)
    cam = ok(client.post("/api/v1/products", headers=H(ot), json={"sku": "CAM1", "name": "Dome Cam", "category_id": cat["id"], "gst_rate": 18, "selling_price": 1500, "reorder_level": 2, "is_serialized": True}), 201)
    cable = ok(client.post("/api/v1/products", headers=H(ot), json={"sku": "CB1", "name": "Cable", "gst_rate": 18, "selling_price": 10, "reorder_level": 5}), 201)
    assert client.post("/api/v1/products", headers=H(ot), json={"sku": "CAM1", "name": "Dup"}).status_code == 409

    # product edit / delete
    upd = ok(client.put(f"/api/v1/products/{cable['id']}", headers=H(ot), json={"sku": "CB1", "name": "Cable 90m", "unit": "MTR", "gst_rate": 18, "selling_price": 12, "reorder_level": 5, "purchase_price": 7, "is_serialized": False}))
    assert upd["name"] == "Cable 90m" and float(upd["purchase_price"]) == 7 and upd["current_stock"] is not None
    extra = ok(client.post("/api/v1/products", headers=H(ot), json={"name": "Temp"}), 201)
    assert client.delete(f"/api/v1/products/{extra['id']}", headers=H(ot)).status_code == 204
    assert client.delete(f"/api/v1/products/{extra['id']}", headers=H(ot)).status_code == 404

    # customers list has outstanding; balance details
    lst = ok(client.get("/api/v1/customers", headers=H(ot)))
    assert {c["name"]: float(c["outstanding"]) for c in lst}["Asha"] == 500
    assert float(ok(client.get(f"/api/v1/suppliers/{sup['id']}", headers=H(ot)))["payable"]) == 100

    # ---- purchase (backdated, serialized) ----
    pur = ok(client.post("/api/v1/purchases", headers=H(ot), json={
        "supplier_id": sup["id"], "purchase_date": "2026-01-15", "items": [
            {"product_id": cam["id"], "quantity": 3, "unit_price": 1000, "gst_rate": 18, "barcodes": ["S1", "S2", "S3"], "supplier_warranty_months": 12},
            {"product_id": cable["id"], "quantity": 100, "unit_price": 7, "gst_rate": 18}]}), 201)
    assert pur["purchase_date"] == "2026-01-15"
    # validation: bad payment mode / zero amount
    assert client.post(f"/api/v1/purchases/{pur['id']}/payments", headers=H(ot), json={"amount": 10, "payment_mode": "BITCOIN"}).status_code == 422
    assert client.post(f"/api/v1/purchases/{pur['id']}/payments", headers=H(ot), json={"amount": 0}).status_code == 422
    p1 = ok(client.post(f"/api/v1/purchases/{pur['id']}/payments", headers=H(ot), json={"amount": 500, "payment_mode": "CHEQUE", "payment_date": "2026-02-01", "reference_no": "CHQ001", "notes": "part"}))
    assert p1["payment_status"] == "PARTIAL"
    hist = ok(client.get(f"/api/v1/purchases/{pur['id']}/payments", headers=H(ot)))
    assert hist[0]["payment_mode"] == "CHEQUE" and hist[0]["reference_no"] == "CHQ001" and hist[0]["payment_date"] == "2026-02-01"
    assert client.post("/api/v1/purchases/00000000-0000-0000-0000-000000000000/payments", headers=H(ot), json={"amount": 5}).status_code == 404
    pf = client.get("/api/v1/purchases", headers=H(ot), params={"payment_status": "PARTIAL", "from": "2026-01-01", "to": "2026-01-31"})
    assert pf.headers["X-Total-Count"] == "1"
    assert client.get("/api/v1/purchases", headers=H(ot), params={"payment_status": "PAID"}).json() == []

    # ---- low stock filter + pagination ----
    prods = client.get("/api/v1/products", headers=H(ot), params={"low_stock": "true"})
    assert prods.status_code == 200
    for i in range(5):
        ok(client.post("/api/v1/customers", headers=H(ot), json={"name": f"Bulk {i}"}), 201)
    pg = client.get("/api/v1/customers", headers=H(ot), params={"limit": 2, "offset": 2})
    assert len(pg.json()) == 2 and int(pg.headers["X-Total-Count"]) == 7
    assert client.get("/api/v1/customers", headers=H(ot), params={"limit": 0}).status_code == 422

    # ---- quotation with valid_till, convert with barcodes ----
    q = ok(client.post("/api/v1/quotations", headers=H(ot), json={"customer_id": cust["id"], "valid_till": "2026-12-31", "items": [
        {"product_id": cam["id"], "quantity": 2, "unit_price": 1500, "gst_rate": 18, "customer_warranty_months": 24},
        {"product_id": cable["id"], "quantity": 10, "unit_price": 12, "gst_rate": 18}]}), 201)
    assert q["valid_till"] == "2026-12-31"
    qd = ok(client.get(f"/api/v1/quotations/{q['id']}", headers=H(ot)))
    assert qd["items"][0]["customer_warranty_months"] in (24, None) and any(i["product_name"] == "Dome Cam" for i in qd["items"])
    assert client.put(f"/api/v1/quotations/{q['id']}/status", headers=H(ot), json={"status": "CONVERTED"}).status_code == 422
    ok(client.put(f"/api/v1/quotations/{q['id']}/status", headers=H(ot), json={"status": "ACCEPTED"}))
    cam_line = [i for i in qd["items"] if i["product_name"] == "Dome Cam"][0]
    # wrong barcode count -> 400 and nothing converted
    bad = client.post(f"/api/v1/invoices/from-quotation/{q['id']}", headers=H(ot), json={"items": [{"quotation_item_id": cam_line["id"], "barcodes": ["S1"]}]})
    assert bad.status_code == 400
    assert ok(client.get(f"/api/v1/quotations/{q['id']}", headers=H(ot)))["status"] == "ACCEPTED"
    inv = ok(client.post(f"/api/v1/invoices/from-quotation/{q['id']}", headers=H(ot), json={"items": [{"quotation_item_id": cam_line["id"], "barcodes": ["S1", "S2"]}]}), 201)
    assert inv["warnings"] == []
    w1 = ok(client.get("/api/v1/warranties/lookup/S1", headers=H(ot)))
    assert w1["status"] == "SOLD" and str(w1["invoice_id"]) == inv["id"] and w1["customer_warranty_months"] == 24 and w1["customer_warranty_valid_until"]
    # second conversion refused
    assert client.post(f"/api/v1/invoices/from-quotation/{q['id']}", headers=H(ot)).status_code == 409
    qd = ok(client.get(f"/api/v1/quotations/{q['id']}", headers=H(ot)))
    assert qd["status"] == "CONVERTED" and qd["converted_invoice_no"] == inv["invoice_no"]
    assert inv["invoice_no"].startswith("BCA/")  # prefix from store profile
    # conversion without body still works; serialized line without barcodes gives a warning
    q2 = ok(client.post("/api/v1/quotations", headers=H(ot), json={"customer_id": cust_igst["id"], "items": [{"product_id": cam["id"], "quantity": 1, "unit_price": 1500, "gst_rate": 18}]}), 201)
    inv2 = ok(client.post(f"/api/v1/invoices/from-quotation/{q2['id']}", headers=H(ot)), 201)
    assert len(inv2["warnings"]) == 1 and inv2["is_interstate"] is True and float(inv2["total_igst"]) > 0
    # invoice detail: quotation ref, product names, barcodes, place of supply
    d = ok(client.get(f"/api/v1/invoices/{inv['id']}", headers=H(ot)))
    assert d["quotation_no"] == q["quotation_no"] and d["place_of_supply_state_code"] == "29" and d["is_interstate"] is False
    assert sorted(d["items"][0]["barcodes"] or d["items"][1]["barcodes"] or []) == ["S1", "S2"]
    assert any(i["product_name"] == "Dome Cam" for i in d["items"])
    # changing the store state code changes GST split
    ok(client.put("/api/v1/store-profile", headers=H(ot), json={"store_name": "BCA CCTV", "state_code": "27", "invoice_prefix": "BCA", "quotation_prefix": "QT"}))
    inv3 = ok(client.post("/api/v1/invoices", headers=H(ot), json={"customer_id": cust["id"], "items": [{"product_id": cable["id"], "quantity": 1, "unit_price": 100, "gst_rate": 18}]}), 201)
    assert inv3["is_interstate"] is True
    ok(client.put("/api/v1/store-profile", headers=H(ot), json={"store_name": "BCA CCTV", "state_code": "29", "invoice_prefix": "BCA", "quotation_prefix": "QT"}))

    # invoice payments + history + filters
    pay = ok(client.post(f"/api/v1/invoices/{inv['id']}/payments", headers=H(ot), json={"amount": 100, "payment_mode": "UPI", "reference_no": "UTR9", "payment_date": "2026-03-01"}))
    assert pay["payment_status"] == "PARTIAL"
    ih = ok(client.get(f"/api/v1/invoices/{inv['id']}/payments", headers=H(ot)))
    assert ih[0]["reference_no"] == "UTR9" and ih[0]["created_by_name"] == "Owner"
    r = client.get("/api/v1/invoices", headers=H(ot), params={"payment_status": "PARTIAL"})
    assert r.headers["X-Total-Count"] == "1"
    r = client.get("/api/v1/invoices", headers=H(ot), params={"from": "2000-01-01", "to": "2100-01-01", "search": "asha"})
    assert int(r.headers["X-Total-Count"]) >= 2
    assert client.get("/api/v1/invoices", headers=H(ot), params={"from": "bad"}).status_code == 400
    assert client.get("/api/v1/reports/gst-summary", headers=H(ot), params={"from": "2026-01-01", "to": "2026-12-31"}).status_code == 200

    # balance after invoices
    b = ok(client.get(f"/api/v1/customers/{cust['id']}/balance", headers=H(ot)))
    assert b["outstanding"] == b["opening_balance"] + b["invoice_due"] and b["opening_balance"] == 500

    # ---- warranty edit / statuses / replacement trail ----
    s3 = ok(client.get("/api/v1/warranties/lookup/S3", headers=H(ot)))
    assert s3["status"] == "IN_STOCK"
    stock_before = float(ok(client.get(f"/api/v1/products/{cam['id']}", headers=H(ot)))["current_stock"])
    fixed = ok(client.put(f"/api/v1/warranties/{s3['id']}", headers=H(ot), json={"barcode": "S3-FIXED", "supplier_warranty_months": 24, "notes": "typo"}))
    assert fixed["barcode"] == "S3-FIXED" and fixed["supplier_warranty_months"] == 24 and fixed["supplier_warranty_valid_until"] == "2028-01-15"
    assert client.put(f"/api/v1/warranties/{s3['id']}", headers=H(ot), json={"status": "SOLD"}).status_code == 400
    assert client.put(f"/api/v1/warranties/{w1['id']}", headers=H(ot), json={"status": "SCRAPPED"}).status_code == 400  # sold: claim first
    scr = ok(client.put(f"/api/v1/warranties/{s3['id']}", headers=H(ot), json={"status": "SCRAPPED"}))
    assert scr["status"] == "SCRAPPED"
    assert float(ok(client.get(f"/api/v1/products/{cam['id']}", headers=H(ot)))["current_stock"]) == stock_before - 1
    back = ok(client.put(f"/api/v1/warranties/{s3['id']}", headers=H(ot), json={"status": "IN_STOCK"}))
    assert back["status"] == "IN_STOCK"
    assert float(ok(client.get(f"/api/v1/products/{cam['id']}", headers=H(ot)))["current_stock"]) == stock_before
    ok(client.put(f"/api/v1/warranties/{w1['id']}", headers=H(ot), json={"customer_warranty_months": 36}))
    assert ok(client.get(f"/api/v1/warranties/{w1['id']}", headers=H(ot)))["customer_warranty_months"] == 36
    dup = client.put(f"/api/v1/warranties/{s3['id']}", headers=H(ot), json={"barcode": "S1"})
    assert dup.status_code == 409
    # claim + replace -> trail on both sides
    ok(client.post(f"/api/v1/warranties/{w1['id']}/claim", headers=H(ot), json={"notes": "dead", "replacement_barcode": "S3-FIXED"}))
    orig = ok(client.get(f"/api/v1/warranties/{w1['id']}", headers=H(ot)))
    repl = ok(client.get("/api/v1/warranties/lookup/S3-FIXED", headers=H(ot)))
    assert orig["status"] == "REPLACED" and orig["replaced_by_barcode"] == "S3-FIXED"
    assert repl["status"] == "SOLD" and repl["replaces_barcode"] == "S1" and str(repl["replaces_warranty_id"]) == str(w1["id"])
    assert client.put(f"/api/v1/warranties/{w1['id']}", headers=H(ot), json={"status": "IN_STOCK"}).status_code == 400
    assert ok(client.get("/api/v1/warranties", headers=H(ot), params={"status": "REPLACED"}))[0]["barcode"] == "S1"
    # expiring: supplier scope, expired handling, count via total header
    ok(client.post("/api/v1/warranties", headers=H(ot), json={"product_id": cam["id"], "barcode": "OLD1", "purchase_date": "2020-01-01", "supplier_warranty_months": 12}), 201)
    e_all = client.get("/api/v1/warranties/expiring", headers=H(ot), params={"days": 30, "scope": "supplier", "limit": 1})
    e_live = client.get("/api/v1/warranties/expiring", headers=H(ot), params={"days": 30, "scope": "supplier", "include_expired": "false", "limit": 1})
    assert int(e_all.headers["X-Total-Count"]) >= 1 and e_live.headers["X-Total-Count"] == "0"
    assert client.get("/api/v1/warranties/expiring", headers=H(ot), params={"scope": "nope"}).status_code == 400

    # ---- AMC: filters, phone, technician ----
    amc = ok(client.post("/api/v1/amc", headers=H(ot), json={"customer_id": cust["id"], "start_date": "2026-01-01", "end_date": "2027-01-01", "amount": 5000}), 201)
    assert client.post("/api/v1/amc", headers=H(ot), json={"customer_id": cust["id"], "start_date": "2026-05-01", "end_date": "2026-01-01", "amount": 1}).status_code == 400
    soon = ok(client.post("/api/v1/amc", headers=H(ot), json={"customer_id": cust["id"], "start_date": "2025-10-01", "end_date": str(__import__("datetime").date.today() + __import__("datetime").timedelta(days=10)), "amount": 1000}), 201)
    assert ok(client.get("/api/v1/amc", headers=H(ot), params={"expiring_within_days": 30}))[0]["id"] == soon["id"]
    assert client.get("/api/v1/amc", headers=H(ot), params={"status": "CANCELLED"}).json() == []
    detail = ok(client.get(f"/api/v1/amc/{amc['id']}", headers=H(ot)))
    assert detail["customer_phone"] == "9111111111"
    v = detail["visits"][0]
    v1 = ok(client.put(f"/api/v1/amc/visits/{v['id']}", headers=H(ot), json={"status": "COMPLETED", "technician_id": tech["id"], "remarks": "ok"}))
    assert v1["technician_name"] == "Tia" and v1["completed_date"]
    v2 = ok(client.put(f"/api/v1/amc/visits/{v['id']}", headers=H(ot), json={"status": "COMPLETED", "remarks": "changed"}))
    assert v2["technician_name"] == "Tia" and v2["remarks"] == "changed"  # technician kept when omitted
    assert client.put(f"/api/v1/amc/visits/{v['id']}", headers=H(ot), json={"status": "COMPLETED", "technician_id": "00000000-0000-0000-0000-000000000000"}).status_code == 400
    assert ok(client.get(f"/api/v1/amc/{amc['id']}", headers=H(ot)))["visits"][0]["technician_name"] == "Tia"

    # ---- field roles may only touch their own (or unassigned) visits ----
    tia = first_login(client, tech)  # Tia is assigned to visit 0
    sam = st2                        # Sam (STAFF), signed in above
    visits = ok(client.get(f"/api/v1/amc/{amc['id']}", headers=H(ot)))["visits"]
    mine, free = visits[0], visits[1]
    assert client.put(f"/api/v1/amc/visits/{mine['id']}", headers=H(sam), json={"status": "MISSED"}).status_code == 404   # Tia's visit
    assert client.put(f"/api/v1/amc/visits/{mine['id']}", headers=H(sam), json={"status": "COMPLETED", "technician_id": staff["id"]}).status_code == 404  # no stealing
    assert client.put("/api/v1/amc/visits/not-a-uuid", headers=H(sam), json={"status": "MISSED"}).status_code == 404
    assert ok(client.put(f"/api/v1/amc/visits/{mine['id']}", headers=H(tia), json={"status": "COMPLETED", "remarks": "tia again"}))["technician_name"] == "Tia"
    assert client.put(f"/api/v1/amc/visits/{free['id']}", headers=H(sam), json={"status": "COMPLETED", "technician_id": tech["id"]}).status_code == 403   # can't hand it to Tia
    assert client.put(f"/api/v1/amc/visits/{free['id']}", headers=H(sam), json={"status": "COMPLETED", "technician_id": None}).status_code == 403          # nor clear it
    taken = ok(client.put(f"/api/v1/amc/visits/{free['id']}", headers=H(sam), json={"status": "COMPLETED", "remarks": "done by Sam"}))
    assert taken["technician_name"] == "Sam"                     # acting on an unassigned visit assigns it to them
    assert client.put(f"/api/v1/amc/visits/{free['id']}", headers=H(tia), json={"status": "MISSED"}).status_code == 404  # now Sam's
    assert ok(client.put(f"/api/v1/amc/visits/{free['id']}", headers=H(ot), json={"status": "SCHEDULED", "technician_id": tech["id"]}))["technician_name"] == "Tia"  # owner can reassign

    # ---- reports still fine ----
    for path in ("dashboard", "low-stock", "outstanding-receivables", "outstanding-payables"):
        assert client.get(f"/api/v1/reports/{path}", headers=H(ot)).status_code == 200
    dash = ok(client.get("/api/v1/reports/dashboard", headers=H(ot)))
    assert len(dash["salesTrend"]) == 7 and isinstance(dash["recentInvoices"], list)
    assert {"previousMonthSales", "topProducts", "lowStockItems", "unpaidInvoiceCount"} <= set(dash)
    # customer/supplier history still fine
    assert ok(client.get(f"/api/v1/customers/{cust['id']}/history", headers=H(ot)))["invoices"]
    assert ok(client.get(f"/api/v1/suppliers/{sup['id']}/history", headers=H(ot)))["purchases"]
    # report lists + histories are paged; totals/aggregates ride in headers
    low = client.get("/api/v1/reports/low-stock", headers=H(ot), params={"limit": 1})
    assert low.status_code == 200 and len(low.json()) <= 1
    assert int(low.headers["x-total-count"]) >= len(low.json()) and "x-out-of-stock-count" in low.headers
    for path in ("outstanding-receivables", "outstanding-payables"):
        r = client.get(f"/api/v1/reports/{path}", headers=H(ot), params={"limit": 1})
        assert r.status_code == 200 and len(r.json()) <= 1
        assert int(r.headers["x-total-count"]) >= len(r.json()) and float(r.headers["x-total-outstanding"]) >= 0
    ch = client.get(f"/api/v1/customers/{cust['id']}/history", headers=H(ot), params={"limit": 1})
    assert ch.status_code == 200 and len(ch.json()["invoices"]) == 1 and int(ch.headers["x-total-count"]) >= 1
    sh = client.get(f"/api/v1/suppliers/{sup['id']}/history", headers=H(ot), params={"limit": 1})
    assert sh.status_code == 200 and len(sh.json()["purchases"]) == 1 and int(sh.headers["x-total-count"]) >= 1
    ex = client.get("/api/v1/amc/expiring", headers=H(ot), params={"days": 30, "limit": 1})
    assert ex.status_code == 200 and len(ex.json()) <= 1 and "x-total-count" in ex.headers
    assert client.get("/api/v1/invoices/{}/pdf".format(inv["id"]), headers=H(ot)).headers["content-type"] == "application/pdf"
    # the logo uploaded above is printed on the invoice; once removed, the invoice prints without it
    assert b"/Subtype /Image" in client.get("/api/v1/invoices/{}/pdf".format(inv["id"]), headers=H(ot)).content
    assert ok(client.delete("/api/v1/store-profile/logo", headers=H(ot)))["has_logo"] is False
    assert client.get("/api/v1/store-profile/logo", headers=H(ot)).status_code == 404
    assert b"/Subtype /Image" not in client.get("/api/v1/invoices/{}/pdf".format(inv["id"]), headers=H(ot)).content


def test_platform_admin_backup_restore(client, tmp_path, monkeypatch):
    from app.config import settings

    # ---- platform admin (bootstrap-admin) and owner (bootstrap-owner) are separate accounts ----
    admin_login = ok(client.post("/api/v1/auth/login", json={"phone": "9000000000", "password": "adminpass1"}))
    assert admin_login["user"]["is_platform_admin"] is True and admin_login["user"]["role"] == "ADMIN"
    at = admin_login["token"]
    admin_id = admin_login["user"]["id"]
    me = ok(client.get("/api/v1/auth/me", headers=H(at)))
    assert me["is_platform_admin"] is True and me["role"] == "ADMIN" and me["phone"] == "9000000000"

    owner_login = ok(client.post("/api/v1/auth/login", json={"phone": "9000000001", "password": "ownerpass1"}))
    assert owner_login["user"]["is_platform_admin"] is False
    ot = owner_login["token"]
    users = ok(client.get("/api/v1/auth/users", headers=H(ot)))
    assert not any(u["is_platform_admin"] for u in users)  # platform admin never appears in the staff list
    assert "Admin" not in [u["name"] for u in users]
    assert "Admin" not in [u["name"] for u in ok(client.get("/api/v1/auth/team", headers=H(ot)))]

    # the platform admin has NO business functionality — only backup/restore
    assert client.get("/api/v1/auth/users", headers=H(at)).status_code == 403       # not an OWNER
    assert client.post("/api/v1/customers", headers=H(at), json={"name": "X"}).status_code == 403
    assert client.put("/api/v1/store-profile", headers=H(at), json={"store_name": "X"}).status_code == 403
    # ... and conversely the owner has no access to admin endpoints — only the platform admin does
    for method, path in (("get", "/api/v1/admin/backup"), ("get", "/api/v1/admin/summary"), ("get", "/api/v1/admin/snapshots"),
                         ("post", "/api/v1/admin/restore?confirm=RESTORE")):
        assert getattr(client, method)(path, headers=H(ot)).status_code == 403, path
    assert client.get("/api/v1/admin/backup").status_code == 401
    # the platform admin account is untouchable, and there is no way to create a second one or a second owner
    assert client.put(f"/api/v1/auth/users/{admin_id}", headers=H(ot), json={"is_active": False}).status_code == 400
    assert client.post(f"/api/v1/auth/users/{admin_id}/reset-password", headers=H(ot), json={"new_password": "hijack1234"}).status_code in (404, 405)
    assert client.post("/api/v1/auth/bootstrap-admin", json={"name": "A2", "phone": "9000000098", "password": "adminpass2"}).status_code == 403
    assert client.post("/api/v1/auth/bootstrap-owner", json={"name": "O2", "phone": "9000000097", "password": "ownerpass2"}).status_code == 403

    # ---- take a backup ----
    summary = ok(client.get("/api/v1/admin/summary", headers=H(at)))
    assert summary["total_rows"] > 50 and summary["tables"]["users"] == 4
    resp = client.get("/api/v1/admin/backup", headers=H(at))
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/octet-stream"
    assert ".dump.enc" in resp.headers["content-disposition"] and resp.content.startswith(b"BCAENC1\n")
    backup = resp.content
    assert b"AfterBackup" not in backup and b"PGDMP" not in backup[:64]  # the test server has a key: ciphertext
    customers_before = client.get("/api/v1/customers", headers=H(ot), params={"limit": 500}).json()
    warranties_before = client.get("/api/v1/warranties", headers=H(ot), params={"limit": 500}).json()
    assert any(w["replaced_by_barcode"] for w in warranties_before)  # self-referencing FKs exist

    # ---- change things after the backup ----
    ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "AfterBackup"}), 201)
    prod = client.get("/api/v1/products", headers=H(ot)).json()[0]

    # ---- bad restores change nothing ----
    assert client.post("/api/v1/admin/restore", headers=H(at), content=backup).status_code == 400                # no confirm
    assert client.post("/api/v1/admin/restore?confirm=RESTORE", headers=H(at), content=b"garbage").status_code == 400
    assert client.post("/api/v1/admin/restore?confirm=RESTORE", headers=H(at), content=b"").status_code == 400
    cut = client.post("/api/v1/admin/restore?confirm=RESTORE", headers=H(at), content=backup[: len(backup) // 2])
    assert cut.status_code == 400, cut.text
    # a file that is neither one of our encrypted backups nor a pg_dump archive is refused
    not_a_dump = client.post("/api/v1/admin/restore?confirm=RESTORE", headers=H(at), content=b"\x1f\x8b gzip but not a backup")
    assert not_a_dump.status_code == 400 and "not a BCAcctvWala backup" in not_a_dump.json()["error"], not_a_dump.text
    flipped = bytearray(backup)
    flipped[len(backup) // 2] ^= 0x01
    assert client.post("/api/v1/admin/restore?confirm=RESTORE", headers=H(at), content=bytes(flipped)).status_code == 400
    names = [c["name"] for c in client.get("/api/v1/customers", headers=H(ot), params={"limit": 500}).json()]
    assert "AfterBackup" in names  # untouched

    # ---- real restore ----
    result = ok(client.post("/api/v1/admin/restore?confirm=RESTORE", headers=H(at), content=backup))
    assert result["snapshot"].startswith("pre-restore-") and result["restored"]["users"] == 4
    # a restore replaces users, so every sign-in ends: both sessions are refused until the users sign in again
    assert client.get("/api/v1/auth/users", headers=H(ot)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=H(at)).status_code == 401
    ot = ok(client.post("/api/v1/auth/login", json={"phone": "9000000001", "password": "ownerpass1"}))["token"]
    at = ok(client.post("/api/v1/auth/login", json={"phone": "9000000000", "password": "adminpass1"}))["token"]
    after = client.get("/api/v1/customers", headers=H(ot), params={"limit": 500}).json()
    assert sorted(c["name"] for c in after) == sorted(c["name"] for c in customers_before)
    assert all(c["name"] != "AfterBackup" for c in after)
    warranties_after = client.get("/api/v1/warranties", headers=H(ot), params={"limit": 500}).json()
    key = lambda w: w["barcode"] or w["id"]
    assert {key(w): (w["status"], w["replaces_barcode"], w["replaced_by_barcode"]) for w in warranties_after} == \
           {key(w): (w["status"], w["replaces_barcode"], w["replaced_by_barcode"]) for w in warranties_before}
    without_audit = lambda tables: {k: v for k, v in tables.items() if k != "audit_logs"}
    assert without_audit(ok(client.get("/api/v1/admin/summary", headers=H(at)))["tables"]) == without_audit(summary["tables"])
    assert "auth_sessions" not in summary["tables"]  # live sign-ins are not part of a backup
    # the audit log comes back with the backup, so what happened after the backup is gone from it ...
    trail = ok(client.get("/api/v1/audit-logs", headers=H(ot), params={"search": "AfterBackup", "limit": 50}))
    assert not any(r["summary"] == "Created customer AfterBackup" for r in trail)
    # ... and the RESTORE itself was written to the restored log afterwards
    admin_trail = ok(client.get("/api/v1/admin/audit-logs", headers=H(at), params={"limit": 200}))
    assert "RESTORE" in {r["action"] for r in admin_trail}
    # numbering / serial columns keep working after a restore
    assert client.post(f"/api/v1/products/{prod['id']}/adjust-stock", headers=H(ot), json={"quantity": 1, "note": "post-restore"}).status_code in (200, 201)
    inv = client.post("/api/v1/invoices", headers=H(ot), json={"customer_id": after[0]["id"], "items": [{"description": "Visit", "quantity": 1, "unit_price": 100, "gst_rate": 18}]})
    assert inv.status_code == 201, inv.text
    assert client.get("/api/v1/auth/users", headers=H(ot)).status_code == 200
    assert ok(client.get("/api/v1/auth/me", headers=H(at)))["is_platform_admin"] is True

    # ---- undo through the server-side safety snapshot ----
    snaps = ok(client.get("/api/v1/admin/snapshots", headers=H(at)))
    assert snaps and snaps[0]["name"] == result["snapshot"]
    assert client.post("/api/v1/admin/snapshots/../../etc/passwd/restore?confirm=RESTORE", headers=H(at)).status_code in (404, 405)
    assert client.post("/api/v1/admin/snapshots/evil.txt/restore?confirm=RESTORE", headers=H(at)).status_code == 404
    r2 = ok(client.post(f"/api/v1/admin/snapshots/{snaps[0]['name']}/restore?confirm=RESTORE", headers=H(at)))
    assert r2["snapshot"] != snaps[0]["name"] and r2["restored"]["customers"] == 8
    ot = ok(client.post("/api/v1/auth/login", json={"phone": "9000000001", "password": "ownerpass1"}))["token"]
    at = ok(client.post("/api/v1/auth/login", json={"phone": "9000000000", "password": "adminpass1"}))["token"]
    names = [c["name"] for c in client.get("/api/v1/customers", headers=H(ot), params={"limit": 500}).json()]
    assert "AfterBackup" in names
    admin_trail = ok(client.get("/api/v1/admin/audit-logs", headers=H(at), params={"limit": 200}))
    # the snapshot was taken right after the BACKUP was logged and before the RESTORE, so its log has the first only
    assert {"BACKUP", "RESTORE_SNAPSHOT"} <= {r["action"] for r in admin_trail}


def test_service_tickets(client, tmp_path, monkeypatch):
    """Raise ticket (owner) -> attend / reschedule with photos (staff / technician) -> close (owner) -> bill.
    Runs last on purpose: it adds rows, and the backup test above asserts exact row counts."""
    from app.config import settings

    from io import BytesIO
    from PIL import Image

    # A real phone-style photo: EXIF with a GPS position of the customer's site.
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (28.0, 24.0, 30.0), "E", (77.0, 18.0, 45.0)
    buf = BytesIO()
    Image.new("RGB", (320, 240), (20, 120, 60)).save(buf, "JPEG", exif=exif)
    jpeg = buf.getvalue()
    assert b"SecretCam" in jpeg

    def photo(name="done.jpg", data=jpeg, mime="image/jpeg"):
        return ("images", (name, data, mime))

    ot = ok(client.post("/api/v1/auth/login", json={"phone": "9000000001", "password": "ownerpass1"}))["token"]

    def login_staff(name, phone, role):
        u = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": name, "phone": phone, "role": role}), 201)
        return u, first_login(client, u)

    tech1, t1 = login_staff("Tech One", "9000000081", "TECHNICIAN")
    tech2, t2 = login_staff("Tech Two", "9000000082", "STAFF")
    cust = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "Ticket Cust", "address": "12 MG Road", "state_code": "29"}), 201)
    other = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "Other Cust", "state_code": "29"}), 201)

    # ---- only the owner raises tickets; input is validated ----
    body = {"customer_id": cust["id"], "service_type": "PAID", "title": "DVR not recording", "charge_amount": 500, "assigned_to": tech1["id"]}
    assert client.post("/api/v1/service-tickets", headers=H(t1), json=body).status_code == 403
    assert client.post("/api/v1/service-tickets", headers=H(ot), json={**body, "title": ""}).status_code == 422
    assert client.post("/api/v1/service-tickets", headers=H(ot), json={**body, "service_type": "FREE"}).status_code == 422
    assert client.post("/api/v1/service-tickets", headers=H(ot), json={**body, "scheduled_date": "2020-01-01"}).status_code == 400
    assert client.post("/api/v1/service-tickets", headers=H(ot), json={**body, "assigned_to": cust["id"]}).status_code == 400  # not a staff login
    assert client.post("/api/v1/service-tickets", headers=H(ot), json={**body, "customer_id": "00000000-0000-0000-0000-000000000000"}).status_code == 404
    paid = ok(client.post("/api/v1/service-tickets", headers=H(ot), json=body), 201)
    assert paid["ticket_no"].startswith("TKT/") and paid["status"] == "OPEN" and paid["site_address"] == "12 MG Road"
    assert float(paid["charge_amount"]) == 500 and paid["assigned_to_name"] == "Tech One"
    free = ok(client.post("/api/v1/service-tickets", headers=H(ot), json={
        "customer_id": cust["id"], "service_type": "IN_WARRANTY", "title": "Camera blurry", "charge_amount": 999}), 201)
    assert free["charge_amount"] is None  # nothing to charge on an in-warranty ticket

    # ---- visibility: field staff see their own + unassigned tickets, never the money ----
    mine = ok(client.get(f"/api/v1/service-tickets/{paid['id']}", headers=H(t1)))
    assert "charge_amount" not in mine and "invoice_no" not in mine
    assert [e["event_type"] for e in mine["events"]] == ["CREATED"]
    assert client.get(f"/api/v1/service-tickets/{paid['id']}", headers=H(t2)).status_code == 404
    assert client.get("/api/v1/service-tickets/not-a-uuid", headers=H(t1)).status_code == 404
    # ---- job card PDF: same visibility as the ticket ----
    jc = client.get(f"/api/v1/service-tickets/{paid['id']}/pdf", headers=H(ot))
    assert jc.status_code == 200 and jc.content.startswith(b"%PDF") and jc.headers["content-type"] == "application/pdf"
    assert client.get(f"/api/v1/service-tickets/{paid['id']}/pdf", headers=H(t1)).content.startswith(b"%PDF")
    assert client.get(f"/api/v1/service-tickets/{paid['id']}/pdf", headers=H(t2)).status_code == 404
    assert client.get("/api/v1/service-tickets/not-a-uuid/pdf", headers=H(t1)).status_code == 404
    ids_t2 = [t["id"] for t in ok(client.get("/api/v1/service-tickets", headers=H(t2)))]
    assert free["id"] in ids_t2 and paid["id"] not in ids_t2
    assert {paid["id"], free["id"]} <= {t["id"] for t in ok(client.get("/api/v1/service-tickets", headers=H(ot)))}
    only_open = client.get("/api/v1/service-tickets", headers=H(ot), params={"status": "OPEN", "customer_id": cust["id"], "search": "dvr"})
    assert [t["id"] for t in only_open.json()] == [paid["id"]] and only_open.headers["X-Total-Count"] == "1"
    assert client.get("/api/v1/service-tickets", headers=H(ot), params={"status": "WHATEVER"}).status_code == 400
    assert client.get("/api/v1/service-tickets", headers=H(t1), params={"billable": "true"}).status_code == 403

    # ---- field staff only: the owner can't attend; photos are required and checked ----
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/attend", headers=H(ot), files=[photo()]).status_code == 403
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/attend", headers=H(t1), data={"remarks": "x"}).status_code == 400  # no photo
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/attend", headers=H(t1), files=[photo("a.jpg", b"not an image")]).status_code == 400
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/attend", headers=H(t1), files=[photo()] * 7).status_code == 400
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/attend", headers=H(t2), files=[photo()]).status_code == 404  # someone else's
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/close", headers=H(ot)).status_code == 409  # not attended yet

    # ---- reschedule ----
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/reschedule", headers=H(ot), json={"new_date": "2099-01-01", "reason": "x"}).status_code == 403
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/reschedule", headers=H(t1), json={"new_date": "2020-01-01", "reason": "late"}).status_code == 400
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/reschedule", headers=H(t1), json={"new_date": "2099-01-01", "reason": ""}).status_code == 422
    resched = ok(client.post(f"/api/v1/service-tickets/{paid['id']}/reschedule", headers=H(t1), json={"new_date": "2099-01-01", "reason": "Customer away"}))
    assert resched["status"] == "RESCHEDULED" and resched["scheduled_date"] == "2099-01-01"

    # ---- attend with two photos ----
    done = ok(client.post(f"/api/v1/service-tickets/{paid['id']}/attend", headers=H(t1), data={"remarks": "Replaced the HDD"}, files=[photo("a.jpg"), photo("b.jpg")]))
    assert done["status"] == "ATTENDED" and done["attended_at"]
    detail = ok(client.get(f"/api/v1/service-tickets/{paid['id']}", headers=H(ot)))
    assert [e["event_type"] for e in detail["events"]] == ["CREATED", "RESCHEDULED", "ATTENDED"]
    attended = detail["events"][-1]
    assert attended["note"] == "Replaced the HDD" and attended["actor_name"] == "Tech One" and len(attended["images"]) == 2
    img_url = f"/api/v1/service-tickets/{paid['id']}/images/{attended['images'][0]['id']}"
    got = client.get(img_url, headers=H(ot))
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    stored = Image.open(BytesIO(got.content))  # re-encoded on upload: same picture, no EXIF / GPS
    assert stored.size == (320, 240) and len(stored.getexif()) == 0 and b"SecretCam" not in got.content
    assert client.get(img_url, headers=H(t1)).status_code == 200
    assert client.get(img_url, headers=H(t2)).status_code == 404  # not their ticket
    assert client.get(img_url).status_code == 401
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/reschedule", headers=H(t1), json={"new_date": "2099-02-01", "reason": "again"}).status_code == 409
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/attend", headers=H(t1), files=[photo()]).status_code == 409  # already attended
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/cancel", headers=H(ot), json={"reason": "nope"}).status_code == 409

    # an unassigned ticket is claimed by whoever attends it
    claimed = ok(client.post(f"/api/v1/service-tickets/{free['id']}/attend", headers=H(t2), files=[photo()]))
    assert claimed["assigned_to"] == tech2["id"]

    # ---- owner reviews and closes (only the owner) ----
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/close", headers=H(t1)).status_code == 403
    closed = ok(client.post(f"/api/v1/service-tickets/{paid['id']}/close", headers=H(ot), json={"note": "Verified"}))
    assert closed["status"] == "CLOSED" and closed["closed_at"]
    assert client.post(f"/api/v1/service-tickets/{paid['id']}/close", headers=H(ot)).status_code == 409
    assert client.put(f"/api/v1/service-tickets/{paid['id']}", headers=H(ot), json={"title": "x"}).status_code == 409  # closed -> read-only

    # ---- cancel: only before it was attended ----
    spare = ok(client.post("/api/v1/service-tickets", headers=H(ot), json={"customer_id": cust["id"], "service_type": "PAID", "title": "Mistake"}), 201)
    assert client.post(f"/api/v1/service-tickets/{spare['id']}/cancel", headers=H(ot), json={"reason": ""}).status_code == 422
    assert ok(client.post(f"/api/v1/service-tickets/{spare['id']}/cancel", headers=H(ot), json={"reason": "Raised twice"}))["status"] == "CANCELLED"

    # ---- edit: owner can fix the type / price / assignee while the work is still open ----
    fix = ok(client.post("/api/v1/service-tickets", headers=H(ot), json={"customer_id": cust["id"], "service_type": "IN_WARRANTY", "title": "Water damage"}), 201)
    upd = ok(client.put(f"/api/v1/service-tickets/{fix['id']}", headers=H(ot), json={"service_type": "PAID", "charge_amount": 750, "assigned_to": tech2["id"]}))
    assert upd["service_type"] == "PAID" and float(upd["charge_amount"]) == 750 and upd["assigned_to_name"] == "Tech Two"
    ev = ok(client.get(f"/api/v1/service-tickets/{fix['id']}", headers=H(ot)))["events"][-1]
    assert ev["event_type"] == "UPDATED" and "Paid service" in ev["note"] and "Tech Two" in ev["note"]
    assert client.put(f"/api/v1/service-tickets/{fix['id']}", headers=H(t2), json={"title": "x"}).status_code == 403

    # ---- v1.27: add forgotten photos / notes after attending (field staff, until the owner closes) ----
    free_url = f"/api/v1/service-tickets/{free['id']}"  # attended by Tech Two above
    assert client.post(f"{free_url}/addendum", headers=H(ot), files=[photo()]).status_code == 403
    assert client.post(f"{free_url}/addendum", headers=H(t1), files=[photo()]).status_code == 404  # someone else's
    assert client.post(f"{free_url}/addendum", headers=H(t2)).status_code == 400  # nothing to add
    assert client.post(f"{free_url}/addendum", headers=H(t2), files=[photo()] * 7).status_code == 400
    assert client.post(f"{free_url}/addendum", headers=H(t2), files=[photo("a.jpg", b"not an image")]).status_code == 400
    assert client.post(f"/api/v1/service-tickets/{fix['id']}/addendum", headers=H(t2), data={"remarks": "x"}).status_code == 409  # not attended yet
    added = ok(client.post(f"{free_url}/addendum", headers=H(t2), data={"remarks": "Cable re-crimped"}, files=[photo()]))
    assert added["status"] == "ATTENDED"  # status unchanged
    ok(client.post(f"{free_url}/addendum", headers=H(t2), data={"remarks": "Customer happy"}))  # a note alone is fine
    evs = ok(client.get(free_url, headers=H(ot)))["events"]
    assert [e["event_type"] for e in evs] == ["CREATED", "ATTENDED", "ADDENDUM", "ADDENDUM"]
    assert evs[2]["note"] == "Cable re-crimped" and evs[2]["actor_name"] == "Tech Two" and len(evs[2]["images"]) == 1
    assert evs[3]["images"] == []
    for _ in range(3):  # 1 + 1 photos so far; the ticket holds at most 20
        ok(client.post(f"{free_url}/addendum", headers=H(t2), files=[photo()] * 6))
    assert client.post(f"{free_url}/addendum", headers=H(t2), files=[photo()]).status_code == 400  # 20 photos already
    assert client.post(f"{free_url}/addendum", headers=H(t2), data={"remarks": "still fine"}).status_code == 200  # notes are not photos

    # ---- v1.27: the owner can send an attended ticket back (reopen) ----
    redo = ok(client.post("/api/v1/service-tickets", headers=H(ot), json={
        "customer_id": cust["id"], "service_type": "IN_WARRANTY", "title": "Redo me", "assigned_to": tech1["id"]}), 201)
    redo_url = f"/api/v1/service-tickets/{redo['id']}"
    assert client.post(f"{redo_url}/reopen", headers=H(ot), json={"reason": "x"}).status_code == 409  # not attended yet
    ok(client.post(f"{redo_url}/attend", headers=H(t1), data={"remarks": "First try"}, files=[photo()]))
    assert client.post(f"{redo_url}/reopen", headers=H(t1), json={"reason": "x"}).status_code == 403  # owner only
    assert client.post(f"{redo_url}/reopen", headers=H(ot), json={"reason": ""}).status_code == 422
    assert client.post(f"{redo_url}/reopen", headers=H(ot), json={"reason": "x", "scheduled_date": "2020-01-01"}).status_code == 400
    back = ok(client.post(f"{redo_url}/reopen", headers=H(ot), json={"reason": "Picture still blurry", "scheduled_date": "2099-03-01"}))
    assert back["status"] == "OPEN" and back["attended_at"] is None and back["scheduled_date"] == "2099-03-01"
    assert client.post(f"{redo_url}/reopen", headers=H(ot), json={"reason": "again"}).status_code == 409  # already open
    assert client.post(f"{redo_url}/addendum", headers=H(t1), data={"remarks": "x"}).status_code == 409  # visit not done now
    d = ok(client.get(redo_url, headers=H(ot)))
    assert [e["event_type"] for e in d["events"]] == ["CREATED", "ATTENDED", "REOPENED"]
    assert d["events"][2]["note"] == "Picture still blurry" and len(d["events"][1]["images"]) == 1  # first visit's photo is kept
    assert client.get(f"{redo_url}/pdf", headers=H(ot)).content.startswith(b"%PDF")  # job card copes with the new events
    ok(client.post(f"{redo_url}/attend", headers=H(t1), data={"remarks": "Second try"}, files=[photo()]))  # the technician goes again
    assert [e["event_type"] for e in ok(client.get(redo_url, headers=H(ot)))["events"]] == ["CREATED", "ATTENDED", "REOPENED", "ATTENDED"]
    assert ok(client.post(f"{redo_url}/close", headers=H(ot)))["status"] == "CLOSED"
    assert client.post(f"{redo_url}/reopen", headers=H(ot), json={"reason": "late"}).status_code == 409  # closed is final
    assert client.post(f"{redo_url}/addendum", headers=H(t1), data={"remarks": "late"}).status_code == 409

    # ---- billing: only attended / closed PAID tickets, owner only, SERVICE invoices only, once ----
    billable = ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"billable": "true", "customer_id": cust["id"]}))
    assert [t["id"] for t in billable] == [paid["id"]]  # not the in-warranty one, not the unattended ones
    line = {"description": "Service: DVR not recording", "quantity": 1, "unit_price": 500, "gst_rate": 18}
    inv_body = {"customer_id": cust["id"], "invoice_type": "SERVICE", "items": [line], "service_ticket_ids": [paid["id"]]}
    assert client.post("/api/v1/invoices", headers=H(t1), json=inv_body).status_code == 403
    assert client.post("/api/v1/invoices", headers=H(ot), json={**inv_body, "invoice_type": "SALE"}).status_code == 400
    assert client.post("/api/v1/invoices", headers=H(ot), json={**inv_body, "customer_id": other["id"]}).status_code == 400  # someone else's ticket
    assert client.post("/api/v1/invoices", headers=H(ot), json={**inv_body, "service_ticket_ids": [free["id"]]}).status_code == 400  # in warranty
    assert client.post("/api/v1/invoices", headers=H(ot), json={**inv_body, "service_ticket_ids": [fix["id"]]}).status_code == 409  # not attended
    assert client.post("/api/v1/invoices", headers=H(ot), json={**inv_body, "service_ticket_ids": ["00000000-0000-0000-0000-000000000000"]}).status_code == 404
    invoice = ok(client.post("/api/v1/invoices", headers=H(ot), json=inv_body), 201)
    billed = ok(client.get(f"/api/v1/service-tickets/{paid['id']}", headers=H(ot)))
    assert billed["invoice_id"] == invoice["id"] and billed["invoice_no"] == invoice["invoice_no"]
    assert billed["events"][-1]["event_type"] == "BILLED"
    assert ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"billable": "true", "customer_id": cust["id"]})) == []
    assert client.post("/api/v1/invoices", headers=H(ot), json=inv_body).status_code == 409  # already billed
    assert [t["ticket_no"] for t in ok(client.get(f"/api/v1/invoices/{invoice['id']}", headers=H(ot)))["service_tickets"]] == [paid["ticket_no"]]
    assert ok(client.get(f"/api/v1/invoices/{invoice['id']}", headers=H(ot)))["items"][0]["description"] == line["description"]

    # ---- v1.27: the ticket's own charge pre-fills the invoice line (include_ticket_charges) ----
    def attended_paid(title, **extra):
        t = ok(client.post("/api/v1/service-tickets", headers=H(ot), json={
            "customer_id": cust["id"], "service_type": "PAID", "title": title, "assigned_to": tech1["id"], **extra}), 201)
        ok(client.post(f"/api/v1/service-tickets/{t['id']}/attend", headers=H(t1), files=[photo()]))
        return t

    priced = attended_paid("Camera realign", charge_amount=300)
    free_charge = attended_paid("No price set")
    auto = {"customer_id": cust["id"], "invoice_type": "SERVICE", "items": [], "include_ticket_charges": True, "service_ticket_ids": [priced["id"]]}
    assert client.post("/api/v1/invoices", headers=H(ot), json={**auto, "service_ticket_ids": None}).status_code == 400  # nothing to take charges from
    assert client.post("/api/v1/invoices", headers=H(ot), json={**auto, "include_ticket_charges": False}).status_code == 400  # no lines at all
    assert client.post("/api/v1/invoices", headers=H(t1), json=auto).status_code == 403
    assert client.post("/api/v1/invoices", headers=H(ot), json={**auto, "service_ticket_ids": [free_charge["id"]]}).status_code == 400  # ticket has no charge
    assert client.post("/api/v1/invoices", headers=H(ot), json={**auto, "service_ticket_ids": [free["id"]]}).status_code == 400  # in warranty
    assert client.post("/api/v1/invoices", headers=H(ot), json={**auto, "service_ticket_ids": [paid["id"]]}).status_code == 409  # already billed
    assert client.post("/api/v1/invoices", headers=H(ot), json={**auto, "customer_id": other["id"]}).status_code == 400  # someone else's ticket
    assert ok(client.get(f"/api/v1/service-tickets/{priced['id']}", headers=H(ot)))["invoice_id"] is None  # nothing was half-billed
    auto_inv = ok(client.post("/api/v1/invoices", headers=H(ot), json=auto), 201)
    assert len(auto_inv["items"]) == 1 and float(auto_inv["total_amount"]) == 354  # 300 + 18% GST
    got = ok(client.get(f"/api/v1/invoices/{auto_inv['id']}", headers=H(ot)))
    assert got["items"][0]["description"] == f"Service {priced['ticket_no']}: Camera realign" and float(got["items"][0]["unit_price"]) == 300
    assert [t["ticket_no"] for t in got["service_tickets"]] == [priced["ticket_no"]]
    # a billed ticket can't be sent back; once the invoice is cancelled it can
    assert client.post(f"/api/v1/service-tickets/{priced['id']}/reopen", headers=H(ot), json={"reason": "x"}).status_code == 409
    ok(client.post(f"/api/v1/invoices/{auto_inv['id']}/cancel", headers=H(ot), json={"reason": "Wrong price"}))
    assert ok(client.post(f"/api/v1/service-tickets/{priced['id']}/reopen", headers=H(ot), json={"reason": "Redo after price change"}))["status"] == "OPEN"
    # extra lines can be added next to the ticket's own line (ticket line first)
    mixed_t = attended_paid("Cabling", charge_amount=200)
    mixed = ok(client.post("/api/v1/invoices", headers=H(ot), json={**auto, "service_ticket_ids": [mixed_t["id"]], "items": [
        {"description": "Extra cable", "quantity": 1, "unit_price": 100, "gst_rate": 18}]}), 201)
    assert [i["description"] for i in mixed["items"]] == [f"Service {mixed_t['ticket_no']}: Cabling", "Extra cable"]
    assert float(mixed["total_amount"]) == 354  # (200 + 100) + 18%

    # ---- v1.28: the invoice PDF names the tickets it bills; customer history lists the customer's tickets ----
    from tests.test_invoice_ticket_ref import _text

    pdf_text = _text(client.get(f"/api/v1/invoices/{mixed['id']}/pdf", headers=H(ot)).content)
    assert "Service ref:" in pdf_text and mixed_t["ticket_no"] in pdf_text
    assert "Service ref:" not in _text(client.get(f"/api/v1/invoices/{auto_inv['id']}/pdf", headers=H(ot)).content)  # un-billed again after the cancel

    hist_url = f"/api/v1/customers/{cust['id']}/history"
    hist = ok(client.get(hist_url, headers=H(ot)))
    assert {paid["ticket_no"], mixed_t["ticket_no"], redo["ticket_no"], priced["ticket_no"]} <= {t["ticket_no"] for t in hist["service_tickets"]}
    assert hist["service_ticket_count"] >= len(hist["service_tickets"]) >= 4
    row = next(t for t in hist["service_tickets"] if t["ticket_no"] == paid["ticket_no"])
    assert row["status"] == "CLOSED" and row["invoice_no"] == invoice["invoice_no"] and float(row["charge_amount"]) == 500
    created = [t["created_at"] for t in hist["service_tickets"]]
    assert created == sorted(created, reverse=True)  # newest first
    beyond = ok(client.get(hist_url, headers=H(ot), params={"limit": 1, "offset": 1000}))  # a page past the last invoice
    assert beyond["invoices"] == [] and len(beyond["service_tickets"]) == len(hist["service_tickets"])
    none_yet = ok(client.get(f"/api/v1/customers/{other['id']}/history", headers=H(ot)))
    assert none_yet["service_tickets"] == [] and none_yet["service_ticket_count"] == 0
    assert client.get(hist_url, headers=H(t1)).status_code == 403  # customers stay owner-only

    # ---- in-warranty tickets are checked against the tracked unit ----
    sup = ok(client.post("/api/v1/suppliers", headers=H(ot), json={"name": "Ticket Dist"}), 201)
    cam = ok(client.post("/api/v1/products", headers=H(ot), json={"sku": "TKCAM", "name": "Ticket Cam", "gst_rate": 18, "selling_price": 1000, "is_serialized": True}), 201)
    ok(client.post("/api/v1/purchases", headers=H(ot), json={"supplier_id": sup["id"], "items": [
        {"product_id": cam["id"], "quantity": 1, "unit_price": 800, "gst_rate": 18, "barcodes": ["TKW-1"], "supplier_warranty_months": 12}]}), 201)
    ok(client.post("/api/v1/invoices", headers=H(ot), json={"customer_id": cust["id"], "items": [
        {"product_id": cam["id"], "quantity": 1, "unit_price": 1000, "gst_rate": 18, "barcodes": ["TKW-1"], "customer_warranty_months": 12}]}), 201)
    wbody = {"customer_id": cust["id"], "service_type": "IN_WARRANTY", "title": "No image", "warranty_barcode": "TKW-1"}
    ok_w = ok(client.post("/api/v1/service-tickets", headers=H(ot), json=wbody), 201)
    assert ok_w["warranty_barcode"] == "TKW-1" and ok_w["warranty_product_name"] == "Ticket Cam"
    assert client.post("/api/v1/service-tickets", headers=H(ot), json={**wbody, "customer_id": other["id"]}).status_code == 400  # sold to someone else
    assert client.post("/api/v1/service-tickets", headers=H(ot), json={**wbody, "warranty_barcode": "NOPE"}).status_code == 404
    unit = ok(client.get("/api/v1/warranties/lookup/TKW-1", headers=H(ot)))
    ok(client.put(f"/api/v1/warranties/{unit['id']}", headers=H(ot), json={"customer_warranty_valid_until": "2020-01-01"}))
    expired = client.post("/api/v1/service-tickets", headers=H(ot), json=wbody)
    assert expired.status_code == 400 and "Paid service" in expired.json()["error"]
    # ...but the same unit can still be raised as a paid service
    ok(client.post("/api/v1/service-tickets", headers=H(ot), json={**wbody, "service_type": "PAID"}), 201)

    # ---- v1.28: tickets raised against a tracked unit show on the unit ----
    unit_view = ok(client.get("/api/v1/warranties/lookup/TKW-1", headers=H(ot)))
    assert [t["service_type"] for t in unit_view["service_tickets"]] == ["PAID", "IN_WARRANTY"]  # newest first
    assert unit_view["service_tickets"][1]["ticket_no"] == ok_w["ticket_no"] and unit_view["service_tickets"][0]["warranty_barcode"] == "TKW-1"
    assert [t["id"] for t in ok(client.get(f"/api/v1/warranties/{unit['id']}", headers=H(ot)))["service_tickets"]] == [t["id"] for t in unit_view["service_tickets"]]
    assert ok(client.get("/api/v1/warranties/lookup/TKW-1", headers=H(ot)))["barcode"] == "TKW-1"  # the unit itself is unchanged

    # ---- v1.28: dashboard numbers and the ticket report ----
    ok(client.put(f"/api/v1/service-tickets/{free_charge['id']}", headers=H(ot), json={"charge_amount": 250}))  # attended, unbilled: 250 waiting to be billed
    import asyncio

    async def make_overdue():
        conn = await raw_connect()
        try:  # the API refuses a visit date in the past, so age one pending ticket directly
            await conn.execute("UPDATE service_tickets SET scheduled_date = CURRENT_DATE - 3 WHERE id = $1", fix["id"])
        finally:
            await conn.close()

    asyncio.run(make_overdue())
    all_t = ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"limit": 500}))

    def count(pred):
        return sum(1 for t in all_t if pred(t))

    dash = ok(client.get("/api/v1/reports/dashboard", headers=H(ot)))
    st = dash["serviceTickets"]
    assert st["pending"] == count(lambda t: t["status"] in ("OPEN", "RESCHEDULED")) >= 4
    assert st["awaitingClose"] == count(lambda t: t["status"] == "ATTENDED") >= 3
    assert st["unbilledPaid"] == count(lambda t: t["service_type"] == "PAID" and t["status"] in ("ATTENDED", "CLOSED") and t["invoice_id"] is None) >= 1
    assert st["overdue"] >= 1 and st["unbilledPaidAmount"] >= 250
    review = dash["ticketsAwaitingClose"]
    assert 1 <= len(review) <= 5 and free_charge["ticket_no"] in {t["ticket_no"] for t in ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"status": "ATTENDED"}))}
    assert [t["attended_at"] for t in review] == sorted(t["attended_at"] for t in review)  # waiting longest first
    assert {"id", "ticket_no", "title", "customer_name", "attended_at"} <= set(review[0])

    rep_url = "/api/v1/reports/service-tickets"
    window = {"from": "2024-01-01", "to": "2033-12-31"}
    rep = ok(client.get(rep_url, headers=H(ot), params=window))
    assert rep["total"] == len(all_t) == sum(rep["by_status"].values()) == sum(rep["by_type"].values())
    assert rep["by_status"]["CANCELLED"] == count(lambda t: t["status"] == "CANCELLED") >= 1
    assert rep["by_type"]["PAID"] == count(lambda t: t["service_type"] == "PAID")
    by_tech = {t["name"]: t for t in rep["by_technician"]}
    assert by_tech["Tech One"]["attended"] >= 1 and by_tech["Tech Two"]["attended"] >= 1 and by_tech["Tech One"]["assigned"] >= by_tech["Tech One"]["attended"]
    assert rep["avg_hours_to_attend"] is not None and rep["avg_hours_to_attend"] >= 0 and rep["avg_hours_to_close"] is not None
    assert rep["overdue"] >= 1 and rep["unbilled_paid"]["count"] == st["unbilledPaid"] and rep["unbilled_paid"]["amount"] >= 250
    empty = ok(client.get(rep_url, headers=H(ot), params={"from": "2001-01-01", "to": "2001-01-31"}))
    assert empty["total"] == 0 and empty["by_technician"] == [] and empty["avg_hours_to_attend"] is None and all(v == 0 for v in empty["by_status"].values())
    assert client.get(rep_url, headers=H(ot)).status_code == 400  # from / to required
    assert client.get(rep_url, headers=H(ot), params={"from": "2026-02-01", "to": "2026-01-01"}).status_code == 400
    assert client.get(rep_url, headers=H(t1), params=window).status_code == 403  # owner only

    # ---- v1.9: PDFs, edit a pending quotation, edit / cancel purchases, cancel invoices ----
    sup9 = ok(client.post("/api/v1/suppliers", headers=H(ot), json={"name": "Cancel Dist"}), 201)
    cam9 = ok(client.post("/api/v1/products", headers=H(ot), json={"sku": "C9", "name": "Cancel Cam", "gst_rate": 18, "selling_price": 1500, "is_serialized": True}), 201)
    cab9 = ok(client.post("/api/v1/products", headers=H(ot), json={"sku": "C9B", "name": "Cancel Cable", "gst_rate": 18, "selling_price": 10}), 201)
    cus9 = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "Cancel Cust", "state_code": "29"}), 201)

    def stock9(product):
        return float(ok(client.get(f"/api/v1/products/{product['id']}", headers=H(ot)))["current_stock"])

    s9 = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Cx", "phone": "9000000077", "role": "STAFF"}), 201)
    s9_token = first_login(client, s9)

    purchase_body = {"supplier_id": sup9["id"], "purchase_date": "2026-04-01", "items": [
        {"product_id": cam9["id"], "quantity": 2, "unit_price": 1000, "gst_rate": 18, "barcodes": ["C9-1", "C9-2"], "supplier_warranty_months": 12},
        {"product_id": cab9["id"], "quantity": 50, "unit_price": 4, "gst_rate": 18}]}
    pu9 = ok(client.post("/api/v1/purchases", headers=H(ot), json=purchase_body), 201)
    pdf = client.get(f"/api/v1/purchases/{pu9['id']}/pdf", headers=H(ot))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["content-type"] == "application/pdf"
    assert client.get("/api/v1/purchases/00000000-0000-0000-0000-000000000000/pdf", headers=H(ot)).status_code == 404

    # purchase header edit (OWNER only): date moves the supplier-warranty clock, null clears a field
    assert client.put(f"/api/v1/purchases/{pu9['id']}", headers=H(s9_token), json={"notes": "x"}).status_code == 403
    ed = ok(client.put(f"/api/v1/purchases/{pu9['id']}", headers=H(ot), json={"purchase_date": "2026-04-10", "supplier_invoice_no": "SI-9", "notes": "n"}))
    assert ed["purchase_date"] == "2026-04-10" and ed["supplier_invoice_no"] == "SI-9" and ed["supplier_name"] == "Cancel Dist"
    w9 = ok(client.get("/api/v1/warranties/lookup/C9-1", headers=H(ot)))
    assert w9["purchase_date"] == "2026-04-10" and w9["supplier_warranty_valid_until"] == "2027-04-10"
    ed2 = ok(client.put(f"/api/v1/purchases/{pu9['id']}", headers=H(ot), json={"supplier_invoice_no": None}))
    assert ed2["supplier_invoice_no"] is None and ed2["notes"] == "n"
    assert client.put(f"/api/v1/purchases/{pu9['id']}", headers=H(ot), json={"supplier_id": "00000000-0000-0000-0000-000000000000"}).status_code == 404

    # quotation: PDF, edit while PENDING, frozen afterwards
    q9 = ok(client.post("/api/v1/quotations", headers=H(ot), json={"customer_id": cus9["id"], "items": [
        {"product_id": cab9["id"], "quantity": 10, "unit_price": 10, "gst_rate": 18}]}), 201)
    qpdf = client.get(f"/api/v1/quotations/{q9['id']}/pdf", headers=H(ot))
    assert qpdf.status_code == 200 and qpdf.content.startswith(b"%PDF")
    revised = {"customer_id": cus9["id"], "valid_till": "2027-01-31", "notes": "rev", "items": [
        {"product_id": cam9["id"], "quantity": 2, "unit_price": 1500, "gst_rate": 18}]}
    e9 = ok(client.put(f"/api/v1/quotations/{q9['id']}", headers=H(ot), json=revised))
    assert e9["valid_till"] == "2027-01-31" and e9["quotation_no"] == q9["quotation_no"] and float(e9["total_amount"]) == 3540
    assert len(ok(client.get(f"/api/v1/quotations/{q9['id']}", headers=H(ot)))["items"]) == 1
    assert client.put(f"/api/v1/quotations/{q9['id']}", headers=H(ot), json={**revised, "items": []}).status_code == 400
    assert client.put("/api/v1/quotations/00000000-0000-0000-0000-000000000000", headers=H(ot), json=revised).status_code == 404
    ok(client.put(f"/api/v1/quotations/{q9['id']}/status", headers=H(ot), json={"status": "ACCEPTED"}))
    assert client.put(f"/api/v1/quotations/{q9['id']}", headers=H(ot), json=revised).status_code == 409

    # invoice: PDF, cancel (OWNER only, refund must be confirmed), everything reversed
    inv9 = ok(client.post("/api/v1/invoices", headers=H(ot), json={"customer_id": cus9["id"], "items": [
        {"product_id": cam9["id"], "quantity": 2, "unit_price": 1500, "gst_rate": 18, "barcodes": ["C9-1", "C9-2"], "customer_warranty_months": 12},
        {"product_id": cab9["id"], "quantity": 5, "unit_price": 10, "gst_rate": 18}]}), 201)
    assert stock9(cam9) == 0 and stock9(cab9) == 45
    assert client.get(f"/api/v1/invoices/{inv9['id']}/pdf", headers=H(ot)).content.startswith(b"%PDF")
    ok(client.post(f"/api/v1/invoices/{inv9['id']}/payments", headers=H(ot), json={"amount": 100}))
    cancel_url = f"/api/v1/invoices/{inv9['id']}/cancel"
    assert client.post(cancel_url, headers=H(s9_token), json={"reason": "x"}).status_code == 403
    assert client.post(cancel_url, headers=H(ot), json={"reason": ""}).status_code == 422
    assert client.post(cancel_url, headers=H(ot), json={"reason": "Customer returned"}).status_code == 409  # money was received
    c9 = ok(client.post(cancel_url, headers=H(ot), json={"reason": "Customer returned", "refund_payments": True}))
    assert c9["status"] == "CANCELLED" and float(c9["paid_amount"]) == 0 and c9["cancel_reason"] == "Customer returned"
    assert stock9(cam9) == 2 and stock9(cab9) == 50
    u9 = ok(client.get("/api/v1/warranties/lookup/C9-1", headers=H(ot)))
    assert u9["status"] == "IN_STOCK" and u9["invoice_id"] is None and u9["customer_warranty_valid_until"] is None
    assert client.post(cancel_url, headers=H(ot), json={"reason": "again"}).status_code == 409
    assert client.post(f"/api/v1/invoices/{inv9['id']}/payments", headers=H(ot), json={"amount": 5}).status_code == 409
    assert abs(sum(float(p["amount"]) for p in ok(client.get(f"/api/v1/invoices/{inv9['id']}/payments", headers=H(ot))))) < 0.001
    lc = client.get("/api/v1/invoices", headers=H(ot), params={"status": "CANCELLED", "search": "Cancel Cust"})
    assert lc.status_code == 200 and lc.headers["X-Total-Count"] == "1" and lc.json()[0]["status"] == "CANCELLED"
    assert client.get("/api/v1/invoices", headers=H(ot), params={"status": "BOGUS"}).status_code == 422
    assert ok(client.get(f"/api/v1/invoices/{inv9['id']}", headers=H(ot)))["status"] == "CANCELLED"
    assert client.get(f"/api/v1/invoices/{inv9['id']}/pdf", headers=H(ot)).status_code == 200  # still printable, stamped CANCELLED
    assert float(ok(client.get(f"/api/v1/customers/{cus9['id']}/balance", headers=H(ot)))["invoice_due"]) == 0

    # a cancelled quotation-invoice re-opens the quotation so it can be converted again
    qc = ok(client.post("/api/v1/quotations", headers=H(ot), json={"customer_id": cus9["id"], "items": [
        {"product_id": cab9["id"], "quantity": 1, "unit_price": 10, "gst_rate": 18}]}), 201)
    invc = ok(client.post(f"/api/v1/invoices/from-quotation/{qc['id']}", headers=H(ot)), 201)
    assert ok(client.get(f"/api/v1/quotations/{qc['id']}", headers=H(ot)))["status"] == "CONVERTED"
    ok(client.post(f"/api/v1/invoices/{invc['id']}/cancel", headers=H(ot), json={"reason": "wrong customer"}))
    qc_after = ok(client.get(f"/api/v1/quotations/{qc['id']}", headers=H(ot)))
    assert qc_after["status"] == "ACCEPTED" and qc_after["converted_invoice_id"] is None
    again = ok(client.post(f"/api/v1/invoices/from-quotation/{qc['id']}", headers=H(ot)), 201)  # can be converted again
    # It holds 1 cable out of stock; cancel it so the purchase below can be taken back in full (50 in, 50 on the shelf).
    ok(client.post(f"/api/v1/invoices/{again['id']}/cancel", headers=H(ot), json={"reason": "test clean-up"}))

    # purchase cancel: refused while a unit is sold or money was paid; then everything goes back
    inv9b = ok(client.post("/api/v1/invoices", headers=H(ot), json={"customer_id": cus9["id"], "items": [
        {"product_id": cam9["id"], "quantity": 1, "unit_price": 1500, "gst_rate": 18, "barcodes": ["C9-1"]}]}), 201)
    pcancel = f"/api/v1/purchases/{pu9['id']}/cancel"
    assert client.post(pcancel, headers=H(s9_token), json={"reason": "x"}).status_code == 403
    assert client.post(pcancel, headers=H(ot), json={"reason": "Entered twice"}).status_code == 409  # C9-1 is with a customer
    ok(client.post(f"/api/v1/invoices/{inv9b['id']}/cancel", headers=H(ot), json={"reason": "undo"}))
    ok(client.post(f"/api/v1/purchases/{pu9['id']}/payments", headers=H(ot), json={"amount": 50}))
    assert client.post(pcancel, headers=H(ot), json={"reason": "Entered twice"}).status_code == 409  # money was paid
    cp = ok(client.post(pcancel, headers=H(ot), json={"reason": "Entered twice", "refund_payments": True}))
    assert cp["status"] == "CANCELLED" and float(cp["paid_amount"]) == 0
    assert stock9(cam9) == 0 and stock9(cab9) == 0
    assert client.get("/api/v1/warranties/lookup/C9-1", headers=H(ot)).status_code == 404  # units removed
    assert client.put(f"/api/v1/purchases/{pu9['id']}", headers=H(ot), json={"notes": "z"}).status_code == 409
    assert client.post(f"/api/v1/purchases/{pu9['id']}/payments", headers=H(ot), json={"amount": 5}).status_code == 409
    assert client.post(pcancel, headers=H(ot), json={"reason": "again"}).status_code == 409
    assert client.get("/api/v1/purchases", headers=H(ot), params={"status": "CANCELLED"}).headers["X-Total-Count"] == "1"
    assert client.get(f"/api/v1/purchases/{pu9['id']}/pdf", headers=H(ot)).status_code == 200
    # the same serials can be entered again, and the cancelled purchase no longer counts as owed
    ok(client.post("/api/v1/purchases", headers=H(ot), json=purchase_body), 201)
    assert float(ok(client.get(f"/api/v1/suppliers/{sup9['id']}/balance", headers=H(ot)))["payable"]) == 2596
    assert client.get("/api/v1/reports/gst-summary", headers=H(ot), params={"from": "2000-01-01", "to": "2100-01-01"}).status_code == 200
    assert client.get("/api/v1/reports/dashboard", headers=H(ot)).status_code == 200
