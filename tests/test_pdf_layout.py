"""v1.36.0: every PDF follows the invoice's header and page pattern (title, boxed shop header, grey-barred boxes,
continuation lines, signatory cell, footer note). No database needed."""
import shutil
import subprocess
from datetime import date, datetime, timedelta

import pytest

from app.utils.pdf import (build_amc_pdf, build_invoice_pdf, build_purchase_pdf, build_quotation_pdf, build_receipt_pdf,
                           build_ticket_pdf, build_warranty_card_pdf)

STORE = {"store_name": "Test Store", "address": "1 Main Rd, Delhi", "phone": "99999", "email": "shop@example.com", "gstin": "07ABCDE1234F1Z5",
         "state_code": "07", "state": "Delhi", "bank_name": "HDFC Bank", "bank_account_no": "12345678", "bank_ifsc": "HDFC0001234"}


def _text(pdf: bytes) -> str:
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, check=True).stdout.decode()


def _line(n=1, **kw):
    return {"description": None, "product_name": f"Camera {n}", "quantity": 1, "unit_price": 1000, "gst_rate": 18, "gst_amount": 180,
            "line_total": 1180, "hsn_code": "85258010", **kw}


QUOTATION = {"quotation_no": "QUO/1", "quotation_date": date(2026, 10, 1), "valid_till": date(2026, 10, 15), "total_amount": 1180,
             "customer_name": "Asha", "customer_address": "12 MG Road", "customer_phone": "88888", "customer_state_code": "07",
             "status": "PENDING", "notes": "Prices valid for 15 days."}
PURCHASE = {"purchase_no": "PUR/1", "purchase_date": date(2026, 10, 1), "supplier_name": "Hik Distributor", "supplier_phone": "77777",
            "supplier_invoice_no": "S-9", "total_amount": 1180, "paid_amount": 500, "payment_status": "PARTIAL", "status": "ACTIVE"}
TICKET = {"ticket_no": "TKT/1", "service_type": "PAID", "status": "OPEN", "title": "DVR not recording", "site_address": "12 MG Road",
          "customer_name": "Asha", "created_at": datetime(2026, 10, 1)}
UNIT = {"barcode": "SN-1", "product_name": "4ch DVR", "customer_name": "Asha", "invoice_no": "INV/1", "sale_date": date(2026, 10, 1),
        "customer_warranty_months": 12, "customer_warranty_valid_until": date(2027, 10, 1)}
CONTRACT = {"contract_no": "AMC/1", "status": "ACTIVE", "customer_name": "Asha", "start_date": date(2026, 10, 1),
            "end_date": date(2027, 9, 30), "visit_frequency": "QUARTERLY", "amount": 12000, "gst_rate": 18}
PAYMENT = {"id": "abcdef12-0000", "amount": 500, "payment_date": date(2026, 10, 5), "payment_mode": "UPI", "reference_no": "UTR1"}
INVOICE = {"invoice_no": "INV/1", "invoice_date": date(2026, 10, 1), "customer_name": "Asha", "total_amount": 1180, "paid_amount": 0,
           "total_cgst": 90, "total_sgst": 90, "total_igst": 0, "status": "ACTIVE"}


def _documents() -> dict[str, tuple[bytes, str]]:
    inv_line = _line(cgst_amount=90, sgst_amount=90, igst_amount=0)
    return {
        "invoice": (build_invoice_pdf(STORE, INVOICE, [inv_line]), "Tax Invoice"),
        "quotation": (build_quotation_pdf(STORE, QUOTATION, [_line()]), "Quotation"),
        "purchase": (build_purchase_pdf(STORE, PURCHASE, [_line()]), "Purchase Record"),
        "job card": (build_ticket_pdf(STORE, TICKET, [], True), "Service Job Card"),
        "warranty": (build_warranty_card_pdf(STORE, UNIT), "Warranty Card"),
        "amc": (build_amc_pdf(STORE, CONTRACT, []), "Annual Maintenance Contract"),
        "receipt": (build_receipt_pdf(STORE, "invoice", INVOICE, PAYMENT, {"name": "Asha"}), "Payment Receipt"),
        "voucher": (build_receipt_pdf(STORE, "purchase", {"purchase_no": "PUR/1", "purchase_date": date(2026, 10, 1), "total_amount": 1180},
                                      PAYMENT, {"name": "Hik"}), "Payment Voucher"),
    }


def test_every_document_has_the_invoice_header_and_footer():
    for name, (pdf, title) in _documents().items():
        text = _text(pdf)
        first_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # centred title on top (the tax invoice also carries "ORIGINAL FOR RECIPIENT" at the right of that line)
        assert first_lines[0] == title or (name == "invoice" and first_lines[0].split()[:2] == title.split()), (name, first_lines[:2])
        assert first_lines[1].startswith("Test Store"), name              # then the boxed shop header
        for part in ("Phone:", "99999", "Email:", "GSTIN:", "07ABCDE1234F1Z5", "State:", "07-Delhi"):
            assert part in text, (name, part)
        assert ("Authorized Signatory" in text or name == "purchase") and "For Test Store:" in text or name == "job card", name
        assert "computer-generated" in text or "not a tax invoice" in text or "Internal purchase record" in text, name


def test_invoice_has_no_gst_column_serial_header_and_original_label():
    text = _text(build_invoice_pdf(STORE, INVOICE, [_line(1, cgst_amount=90, sgst_amount=90, igst_amount=0)]))
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0].split()[0:2] == ["Tax", "Invoice"] and lines[0].rstrip().endswith("ORIGINAL FOR RECIPIENT")
    header = next(ln for ln in lines if "Item Name" in ln)
    assert "S.No." in header and "GST" not in header and "#" not in header
    assert "Price/ Unit" in header and "Amount" in header
    # the other item-table documents keep their GST column and "#"
    quote_header = next(ln for ln in _text(build_quotation_pdf(STORE, QUOTATION, [_line(1)])).splitlines() if "Item Name" in ln)
    assert "GST" in quote_header and "S.No." not in quote_header


def test_job_card_has_customer_and_technician_cells():
    text = _text(build_ticket_pdf(STORE, TICKET, [], True))
    assert "Customer Signature" in text and "Technician Signature" in text


def test_long_documents_continue_the_way_the_invoice_does():
    long_ticket = build_ticket_pdf(STORE, TICKET, [{"event_type": "UPDATED", "note": "Called the customer " * 12, "created_at": datetime(2026, 10, 2),
                                                    "actor_name": "Ravi", "scheduled_date": None}] * 40, True)
    visits = [{"scheduled_date": date(2026, 10, 1) + timedelta(days=7 * n), "status": "SCHEDULED", "completed_date": None} for n in range(40)]
    long_amc = build_amc_pdf(STORE, {**CONTRACT, "coverage_notes": "8 cameras, DVR, cabling\n" * 80}, visits)
    long_quote = build_quotation_pdf(STORE, QUOTATION, [_line(n) for n in range(60)])
    for name, (pdf, label) in {"job card": (long_ticket, "Ticket No: TKT/1"), "amc": (long_amc, "Contract No: AMC/1"),
                               "quotation": (long_quote, "Quotation No: QUO/1")}.items():
        text = _text(pdf)
        assert "Continued on next page..." in text, name
        assert f"{label} (continued)" in " ".join(text.split()), name
        assert text.count("Phone:") == 1, name                              # the shop box is only on the first page


def test_quotation_shows_bank_notes_and_no_payment_rows():
    text = _text(build_quotation_pdf(STORE, QUOTATION, [_line()]))
    assert "Quotation For:" in text and "Quotation Details:" in text and "Valid Till:" in text
    assert "Bank Details:" in text and "HDFC Bank" in text and "Prices valid for 15 days." in text
    assert "Tax Summary:" in text and "Quotation Amount In Words" in text
    assert "Received" not in text and "Balance" not in text


def test_purchase_record_has_supplier_paid_and_balance_but_no_bank():
    text = _text(build_purchase_pdf(STORE, PURCHASE, [_line()]))
    assert "Supplier:" in text and "Hik Distributor" in text and "S-9" in text
    assert "Paid" in text and "Balance" in text and "Bank Details:" not in text


def test_quotation_and_purchase_split_gst_by_state():
    intra = _text(build_quotation_pdf(STORE, QUOTATION, [_line()]))
    assert "CGST" in intra and "SGST" in intra and "IGST" not in intra
    inter = _text(build_quotation_pdf(STORE, {**QUOTATION, "total_igst": 180, "customer_state_code": "29"}, [_line()]))
    assert "IGST" in inter and "CGST" not in inter
    assert "IGST" in _text(build_purchase_pdf(STORE, {**PURCHASE, "supplier_state_code": "27"}, [_line()]))
    assert "CGST" in _text(build_purchase_pdf(STORE, {**PURCHASE, "supplier_state_code": "07"}, [_line()]))


def test_cancelled_documents_still_print_and_bad_images_never_stop_them():
    assert build_purchase_pdf(STORE, {**PURCHASE, "status": "CANCELLED"}, [_line()], b"garbage", b"garbage").startswith(b"%PDF")
    assert build_quotation_pdf({}, QUOTATION, [], b"garbage", b"garbage").startswith(b"%PDF")
    assert build_receipt_pdf({}, "invoice", {**INVOICE, "status": "CANCELLED"}, PAYMENT, {"name": "A"}, b"x", b"y").startswith(b"%PDF")
