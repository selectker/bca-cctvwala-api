"""Warranty card PDF renders for typical and sparse units. No database needed."""
from datetime import date

from app.utils.pdf import build_warranty_card_pdf

STORE = {"store_name": "Test Store", "address": "1 Main Rd", "phone": "99999"}
UNIT = {
    "barcode": "SN-12345", "product_name": "4ch DVR", "product_sku": "DVR4", "customer_name": "Asha",
    "customer_phone": "88888", "customer_address": "12 MG Road\nBengaluru", "invoice_no": "INV/2026/0007",
    "invoice_date": date(2026, 10, 1), "sale_date": date(2026, 10, 1), "customer_warranty_months": 12,
    "customer_warranty_valid_until": date(2027, 10, 1), "notes": None, "replaces_barcode": None,
}


def test_card_renders_full_and_sparse():
    assert build_warranty_card_pdf(STORE, UNIT).startswith(b"%PDF")
    assert build_warranty_card_pdf(STORE, {**UNIT, "customer_warranty_months": 1, "replaces_barcode": "OLD-1", "notes": "x " * 400}).startswith(b"%PDF")
    bare = {"customer_name": "A", "customer_warranty_valid_until": date(2027, 1, 1)}
    assert build_warranty_card_pdf({}, bare).startswith(b"%PDF")
    assert build_warranty_card_pdf(STORE, UNIT, b"garbage", b"garbage").startswith(b"%PDF")
