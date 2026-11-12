"""v1.31.0: shop UPI ID, the pay-by-QR code on invoices, and GET /invoices/{id}/upi-link."""
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils.pdf import build_invoice_pdf
from app.utils.upi import upi_payment_uri

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
LINE = {"description": "Installation", "quantity": 1, "unit_price": 1000, "gst_rate": 18}  # Rs. 1180.00


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


def set_profile(client, owner, **extra):
    r = client.put("/api/v1/store-profile", headers=H(owner), json={"store_name": "UPI Shop", "state_code": "29", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def new_invoice(client, owner):
    c = client.post("/api/v1/customers", headers=H(owner), json={"name": f"Upi {time.time_ns()}", "state_code": "29"}).json()
    r = client.post("/api/v1/invoices", headers=H(owner), json={"customer_id": c["id"], "items": [LINE]})
    assert r.status_code == 201, r.text
    return r.json()


def test_link_format():
    uri = upi_payment_uri("shop@okhdfc", "BCA CCTV & Co", 680, "INV/2026/1")
    assert uri == "upi://pay?pa=shop@okhdfc&pn=BCA%20CCTV%20%26%20Co&am=680.00&cu=INR&tn=INV%2F2026%2F1"


def test_upi_id_validation_and_partial_update(client, owner):
    assert set_profile(client, owner, upi_id="  shop@okhdfc ")["upi_id"] == "shop@okhdfc"
    for bad in ["shop", "shop@", "@bank", "sh op@bank", "a@b", "shop@bank@x", "x" * 200]:
        r = client.put("/api/v1/store-profile", headers=H(owner), json={"store_name": "S", "upi_id": bad})
        assert r.status_code == 422, bad
    # an older app that doesn't send upi_id leaves it alone; "" / null clears it
    assert set_profile(client, owner)["upi_id"] == "shop@okhdfc"
    assert set_profile(client, owner, upi_id="")["upi_id"] is None
    assert set_profile(client, owner, upi_id="a.b-c_d@ybl")["upi_id"] == "a.b-c_d@ybl"
    assert set_profile(client, owner, upi_id=None)["upi_id"] is None


def test_upi_link_endpoint(client, owner):
    inv = new_invoice(client, owner)
    url = f"/api/v1/invoices/{inv['id']}/upi-link"

    set_profile(client, owner, upi_id=None)
    assert client.get(url, headers=H(owner)).status_code == 400  # no UPI ID saved

    set_profile(client, owner, upi_id="shop@okhdfc")
    r = client.get(url, headers=H(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert float(body["amount"]) == 1180 and body["upi_id"] == "shop@okhdfc"
    assert body["upi_uri"].startswith("upi://pay?pa=shop@okhdfc&pn=UPI%20Shop&am=1180.00&cu=INR&tn=")

    # after a part payment the link is for the balance
    assert client.post(f"/api/v1/invoices/{inv['id']}/payments", headers=H(owner), json={"amount": 500}).status_code == 200
    assert "am=680.00" in client.get(url, headers=H(owner)).json()["upi_uri"]

    # fully paid: nothing to pay; cancelled: refused; unknown: 404
    assert client.post(f"/api/v1/invoices/{inv['id']}/payments", headers=H(owner), json={"amount": 680}).status_code == 200
    assert client.get(url, headers=H(owner)).status_code == 400
    other = new_invoice(client, owner)
    assert client.post(f"/api/v1/invoices/{other['id']}/cancel", headers=H(owner), json={"reason": "x"}).status_code == 200
    assert client.get(f"/api/v1/invoices/{other['id']}/upi-link", headers=H(owner)).status_code == 409
    assert client.get("/api/v1/invoices/00000000-0000-0000-0000-000000000000/upi-link", headers=H(owner)).status_code == 404


def test_upi_id_is_owner_only_and_pdf_builds(client, owner):
    set_profile(client, owner, upi_id="shop@okhdfc")
    phone = f"96{str(time.time_ns())[-8:]}"
    staff = client.post("/api/v1/auth/users", headers=H(owner), json={"name": "S", "phone": phone, "role": "STAFF"}).json()
    from tests.helpers import first_login
    token = first_login(client, staff)
    assert "upi_id" not in client.get("/api/v1/store-profile", headers=H(token)).json()
    assert "upi_id" in client.get("/api/v1/store-profile", headers=H(owner)).json()
    inv = new_invoice(client, owner)
    assert client.get(f"/api/v1/invoices/{inv['id']}/upi-link", headers=H(token)).status_code == 403
    pdf = client.get(f"/api/v1/invoices/{inv['id']}/pdf", headers=H(owner))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def _pdf_text(pdf: bytes) -> str:
    return subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True).stdout.decode("utf-8", "replace")


@pytest.mark.skipif(subprocess.run(["which", "pdftotext"], capture_output=True).returncode != 0, reason="pdftotext not installed")
def test_qr_only_when_something_is_owed_on_a_live_invoice():
    store = {"store_name": "S", "upi_id": "shop@okhdfc"}
    base = {"invoice_no": "I-1", "invoice_date": "2026-10-20", "customer_name": "A", "total_amount": 118, "paid_amount": 0,
            "subtotal": 100, "total_cgst": 9, "total_sgst": 9, "total_igst": 0}
    assert "Scan to pay" in _pdf_text(build_invoice_pdf(store, base, []))
    assert "Scan to pay" not in _pdf_text(build_invoice_pdf(store, {**base, "paid_amount": 118}, []))
    assert "Scan to pay" not in _pdf_text(build_invoice_pdf(store, {**base, "status": "CANCELLED"}, []))
    assert "Scan to pay" not in _pdf_text(build_invoice_pdf({"store_name": "S"}, base, []))
    # the QR and a saved signature share the page without crashing the layout
    from io import BytesIO

    from PIL import Image
    png = BytesIO()
    Image.new("RGBA", (200, 80), (0, 0, 0, 255)).save(png, "PNG")
    many = [{"description": f"Item {i}", "quantity": 1, "unit_price": 10, "gst_rate": 18, "cgst_amount": 0.9,
             "sgst_amount": 0.9, "igst_amount": 0, "line_total": 11.8} for i in range(60)]
    for items in ([], many):  # a short invoice, and one long enough that the QR / signature spill onto a new page
        assert build_invoice_pdf({**store, "bank_name": "HDFC"}, base, items, None, png.getvalue()).startswith(b"%PDF")
