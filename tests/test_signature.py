"""Base64 signature (v1.16.0): decoding, normalising, the invoice PDF, request limits. No database needed."""
import base64
import re
from datetime import date
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.body_limit import limit_for
from app.config import settings
from app.main import app
from app.utils.pdf import build_invoice_pdf, build_quotation_pdf
from app.utils.signature import SignatureError, decode_base64, normalise_signature

MB = 1024 * 1024
STORE = {"store_name": "BCA CCTV", "address": "MG Road, Faridabad", "phone": "9999999999", "bank_name": "HDFC",
         "bank_account_no": "123456789012", "bank_ifsc": "HDFC0001234"}
INVOICE = {"invoice_no": "INV/1", "invoice_date": date(2026, 10, 15), "customer_name": "Asha", "subtotal": 100,
           "total_cgst": 9, "total_sgst": 9, "total_igst": 0, "round_off": 0, "total_amount": 118, "paid_amount": 0}
ITEMS = [{"unit_price": 100, "quantity": 1, "line_total": 118, "gst_rate": 18, "cgst_amount": 9, "sgst_amount": 9,
          "igst_amount": 0, "description": "Dome cam"}]


def _png(size=(400, 160), pad=60, ink=(10, 10, 120, 255)) -> bytes:
    """A transparent canvas with a block of ink inside, so there is a margin to trim."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (size[0] - 2 * pad, size[1] - 2 * pad), ink), (pad, pad))
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_decodes_raw_base64_data_urls_and_line_breaks():
    raw = _png()
    b64 = _b64(raw)
    assert decode_base64(b64) == raw
    assert decode_base64("data:image/png;base64," + b64) == raw
    assert decode_base64(b64[:40] + "\n" + b64[40:] + "  ") == raw
    assert decode_base64(b64.rstrip("=")) == raw  # missing padding is tolerated


@pytest.mark.parametrize("text", ["", "   ", "!!!not base64!!!", "data:image/png;base64,"])
def test_rejects_bad_base64(text):
    with pytest.raises(SignatureError):
        decode_base64(text)


def test_normalise_trims_margins_and_stores_a_png():
    out = Image.open(BytesIO(normalise_signature(_png())))
    assert out.format == "PNG" and out.size == (280, 40)  # 400x160 minus 60px on every side


def test_normalise_trims_a_white_page_background():
    img = Image.new("RGB", (500, 200), "white")
    img.paste(Image.new("RGB", (200, 50), (0, 0, 0)), (100, 60))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=95)
    out = Image.open(BytesIO(normalise_signature(buf.getvalue())))
    assert 195 <= out.width <= 215 and 45 <= out.height <= 65


def test_normalise_shrinks_big_images_and_keeps_shape():
    out = Image.open(BytesIO(normalise_signature(_png((3000, 1200), pad=0))))
    assert out.size == (600, 240)


@pytest.mark.parametrize("data", [b"not an image", b"%PDF-1.4 hello", b"GIF89a" + b"0" * 64])
def test_rejects_non_images(data):
    with pytest.raises(SignatureError):
        normalise_signature(data)


def test_invoice_pdf_with_signature_is_valid_and_bigger():
    sig = normalise_signature(_png())
    plain = build_invoice_pdf(STORE, INVOICE, ITEMS)
    signed = build_invoice_pdf(STORE, INVOICE, ITEMS, None, sig)
    assert plain.startswith(b"%PDF") and signed.startswith(b"%PDF") and len(signed) > len(plain)


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page\b(?!s)", pdf))


def test_signature_cell_is_part_of_the_fixed_footer_and_long_invoices_paginate():
    sig = normalise_signature(_png())
    # the signatory cell is always in the footer of the LAST page, so a signature never pushes the footer onto a page of its own
    assert _pages(build_invoice_pdf(STORE, INVOICE, ITEMS, None, sig)) == 1
    assert _pages(build_invoice_pdf(STORE, INVOICE, ITEMS * 18, None, sig)) == _pages(build_invoice_pdf(STORE, INVOICE, ITEMS * 18))
    many = ITEMS * 120  # runs onto more pages; totals and the signature are printed once, on the last one
    assert _pages(build_invoice_pdf(STORE, INVOICE, many, None, sig)) >= 3


def test_a_broken_signature_never_stops_an_invoice_printing():
    assert build_invoice_pdf(STORE, INVOICE, ITEMS, None, b"garbage").startswith(b"%PDF")


def test_signature_endpoint_has_its_own_body_limit():
    assert limit_for("/api/v1/store-profile/signature") == (settings.max_signature_mb * 4 // 3 + 1) * MB
    assert limit_for("/api/v1/store-profile") == settings.max_body_kb * 1024


def test_oversized_signature_request_is_refused_without_being_read():
    big = limit_for("/api/v1/store-profile/signature") + 1
    r = TestClient(app).put("/api/v1/store-profile/signature", content=b"x" * big,
                            headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_signature_base64_is_never_written_to_the_audit_log():
    from app.utils.audit import sanitize
    assert sanitize({"signature_base64": "AAAA"}) == {"signature_base64": "***"}


def test_purchase_record_never_carries_the_shop_signature():
    from app.utils.pdf import build_purchase_pdf
    purchase = {"purchase_no": "PUR/1", "purchase_date": date(2026, 10, 1), "supplier_name": "Hik", "total_amount": 118,
                "paid_amount": 0, "status": "ACTIVE"}
    sig = normalise_signature(_png())
    with_sig = build_purchase_pdf(STORE, purchase, ITEMS, None, sig)
    without = build_purchase_pdf(STORE, purchase, ITEMS)
    assert len(with_sig) == len(without)  # the image is not embedded even if one is passed
    assert b"Authorized Signatory" not in with_sig
