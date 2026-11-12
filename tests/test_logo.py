"""Shop logo (v1.15.0): image normalising, the PDF header, request limits. No database needed."""
from datetime import date
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.body_limit import limit_for
from app.config import settings
from app.main import app
from app.utils.logo import LogoError, normalise_logo
from app.utils.pdf import build_invoice_pdf, build_quotation_pdf

MB = 1024 * 1024


def _image(fmt="PNG", size=(900, 300), mode="RGBA", color=(20, 80, 200, 255)) -> bytes:
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


STORE = {"store_name": "BCA CCTV", "address": "MG Road, Faridabad " * 8, "phone": "9999999999", "gstin": "29ABCDE1234F1Z5"}
INVOICE = {"invoice_no": "INV/1", "invoice_date": date(2026, 10, 15), "customer_name": "Asha", "subtotal": 100,
           "total_cgst": 9, "total_sgst": 9, "total_igst": 0, "round_off": 0, "total_amount": 118, "paid_amount": 0}
ITEMS = [{"unit_price": 100, "quantity": 1, "line_total": 118, "gst_rate": 18, "cgst_amount": 9, "sgst_amount": 9,
          "igst_amount": 0, "description": "Dome cam"}]
QUOTATION = {"quotation_no": "Q/1", "quotation_date": date(2026, 10, 15), "customer_name": "Asha", "status": "DRAFT",
             "subtotal": 100, "total_amount": 118, "total_gst": 18, "total_cgst": 9, "total_sgst": 9, "total_igst": 0}
Q_ITEMS = [{"quantity": 1, "unit_price": 100, "gst_rate": 18, "gst_amount": 18, "line_total": 118, "description": "Dome cam"}]


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGBA"), ("JPEG", "RGB"), ("WEBP", "RGB")])
def test_accepts_png_jpeg_webp_and_stores_a_small_png(fmt, mode):
    out = normalise_logo(_image(fmt, (2400, 800), mode, (20, 80, 200, 255) if mode == "RGBA" else (20, 80, 200)))
    img = Image.open(BytesIO(out))
    assert img.format == "PNG" and max(img.size) == 600 and img.size == (600, 200)  # shrunk, shape kept


def test_small_logo_is_not_enlarged():
    assert Image.open(BytesIO(normalise_logo(_image(size=(120, 40))))).size == (120, 40)


@pytest.mark.parametrize("data", [b"", b"not an image", b"%PDF-1.4 hello", b"GIF89a" + b"0" * 64, _image("GIF", mode="P", color=1)])
def test_rejects_anything_that_is_not_png_jpeg_webp(data):
    with pytest.raises(LogoError):
        normalise_logo(data)


def test_rejects_a_truncated_image():
    with pytest.raises(LogoError):
        normalise_logo(_image()[:-40])


def test_pdf_with_logo_is_bigger_and_valid():
    logo = normalise_logo(_image())
    plain = build_invoice_pdf(STORE, INVOICE, ITEMS)
    with_logo = build_invoice_pdf(STORE, INVOICE, ITEMS, logo)
    assert plain.startswith(b"%PDF") and with_logo.startswith(b"%PDF") and len(with_logo) > len(plain)
    q_plain, q_logo = build_quotation_pdf(STORE, QUOTATION, Q_ITEMS), build_quotation_pdf(STORE, QUOTATION, Q_ITEMS, logo)
    assert q_logo.startswith(b"%PDF") and len(q_logo) > len(q_plain)


def test_a_broken_logo_never_stops_a_document_printing():
    assert build_invoice_pdf(STORE, INVOICE, ITEMS, b"garbage").startswith(b"%PDF")
    assert build_quotation_pdf(STORE, QUOTATION, Q_ITEMS, b"garbage").startswith(b"%PDF")


def test_logo_upload_has_its_own_body_limit():
    assert limit_for("/api/v1/store-profile/logo") == (settings.max_logo_mb + 1) * MB
    assert limit_for("/api/v1/store-profile") == settings.max_body_kb * 1024


def test_oversized_logo_upload_is_refused_without_being_read():
    r = TestClient(app).post("/api/v1/store-profile/logo", content=b"x" * (limit_for("/api/v1/store-profile/logo") + 1),
                             headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413
