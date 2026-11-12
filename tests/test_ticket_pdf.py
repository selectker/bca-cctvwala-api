"""Job card PDF: renders for every ticket state, never leaks money to field staff. No database needed."""
from datetime import date, datetime

from app.utils.pdf import build_ticket_pdf

STORE = {"store_name": "Test Store", "address": "1 Main Rd", "phone": "99999"}
TICKET = {
    "ticket_no": "TKT/2026/0001", "service_type": "PAID", "status": "OPEN", "title": "DVR not recording",
    "description": "Hard disk clicks.\nCustomer says it stopped last night. " * 12,
    "site_address": "12 MG Road", "customer_name": "Asha", "customer_phone": "88888",
    "created_at": datetime(2026, 10, 1, 10, 0), "scheduled_date": date(2026, 10, 5), "assigned_to_name": "Ravi",
    "charge_amount": 1500, "gst_rate": 18, "invoice_no": "INV/1", "warranty_barcode": "ABC123",
    "warranty_product_name": "4ch DVR", "warranty_valid_until": date(2027, 1, 1),
}
EVENTS = [{"event_type": "CREATED", "note": None, "scheduled_date": None, "created_at": datetime(2026, 10, 1), "actor_name": "Owner"}] + [
    {"event_type": "RESCHEDULED", "note": "Customer away " * 20, "scheduled_date": date(2026, 10, 9),
     "created_at": datetime(2026, 10, 2), "actor_name": "Ravi"} for _ in range(40)
]


def test_job_card_renders_in_every_state_and_spans_pages():
    for status in ("OPEN", "RESCHEDULED", "ATTENDED", "CLOSED", "CANCELLED"):
        pdf = build_ticket_pdf(STORE, {**TICKET, "status": status}, EVENTS, True)
        assert pdf.startswith(b"%PDF")
    import re
    assert len(re.findall(rb"/Type /Page\b(?!s)", build_ticket_pdf(STORE, TICKET, EVENTS, True))) >= 2


def test_job_card_handles_missing_optional_parts():
    bare = {k: TICKET[k] for k in ("ticket_no", "service_type", "status", "title", "customer_name", "created_at")}
    assert build_ticket_pdf({}, bare, [], False).startswith(b"%PDF")
    assert build_ticket_pdf(STORE, {**TICKET, "service_type": "IN_WARRANTY", "charge_amount": None}, [], True).startswith(b"%PDF")
    assert build_ticket_pdf(STORE, TICKET, [], True, b"garbage").startswith(b"%PDF")


def test_job_card_for_field_staff_has_no_money():
    import base64, re, zlib

    def text(pdf: bytes) -> str:
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

    owner = text(build_ticket_pdf(STORE, TICKET, [], True))
    assert "Estimate" in owner and "1,770.00" in owner and "INV/1" in owner  # 1500 + 18% GST
    staff = text(build_ticket_pdf(STORE, TICKET, [], False))
    assert "DVR not recording" in staff  # the extraction works, so the absences below mean something
    assert "Estimate" not in staff and "Charges" not in staff and "INV/1" not in staff and "1,770" not in staff and "1770" not in staff
