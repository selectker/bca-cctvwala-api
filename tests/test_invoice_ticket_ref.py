"""Invoice PDF shows the service tickets it bills (v1.28.0). No database needed."""
import base64
import re
import zlib
from datetime import date

from app.utils.pdf import build_invoice_pdf

STORE = {"store_name": "Test Store", "address": "1 Main Rd", "phone": "99999"}
INVOICE = {
    "invoice_no": "INV/2026-27/0007", "invoice_date": date(2026, 10, 5), "place_of_supply_state_code": "29",
    "customer_name": "Asha", "billing_address": "12 MG Road", "subtotal": 300, "total_cgst": 27, "total_sgst": 27,
    "total_igst": 0, "round_off": 0, "total_amount": 354, "paid_amount": 0, "status": "ACTIVE",
}
ITEMS = [{"description": "Service TKT/2026-27/0001: Camera realign", "quantity": 1, "unit_price": 300, "gst_rate": 18,
          "cgst_amount": 27, "sgst_amount": 27, "igst_amount": 0, "line_total": 354, "discount_amount": 0}]


def _text(pdf: bytes) -> str:
    # reportlab writes page streams as ASCII85 + Flate
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        body = m.group(1).strip()
        if body.endswith(b"~>"):
            try:
                out += zlib.decompress(base64.a85decode(body[:-2]))
            except Exception:
                pass
    return out.decode("latin-1")


def test_invoice_pdf_lists_the_billed_tickets():
    pdf = build_invoice_pdf(STORE, INVOICE, ITEMS, ticket_nos=["TKT/2026-27/0001", "TKT/2026-27/0002"])
    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    assert "Service ref:" in text and "TKT/2026-27/0001, TKT/2026-27/0002" in text


def test_invoice_pdf_without_tickets_is_unchanged():
    for none in (None, []):
        text = _text(build_invoice_pdf(STORE, INVOICE, ITEMS, ticket_nos=none))
        assert "Service ref:" not in text and "INV/2026-27/0007" in text  # extraction works, the absence means something


def test_a_long_ticket_list_is_cut_to_two_lines():
    many = [f"TKT/2026-27/{n:04d}" for n in range(1, 60)]
    text = _text(build_invoice_pdf(STORE, INVOICE, ITEMS, ticket_nos=many))
    assert "Service ref:" in text and "TKT/2026-27/0001" in text and "TKT/2026-27/0059" not in text and "..." in text
