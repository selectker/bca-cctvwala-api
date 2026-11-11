from app.utils.pdf import build_invoice_pdf, invoice_addresses, _wrap_lines


def test_shipping_same_as_billing_when_empty_or_equal():
    assert invoice_addresses({"billing_address": "A 1", "shipping_address": None}) == ("A 1", "A 1", True)
    assert invoice_addresses({"billing_address": "A  1", "shipping_address": "A 1"})[2] is True


def test_different_shipping_address_is_kept():
    billing, shipping, same = invoice_addresses({"billing_address": "Shop 4", "shipping_address": "Warehouse 7"})
    assert (billing, shipping, same) == ("Shop 4", "Warehouse 7", False)


def test_old_invoice_falls_back_to_customer_address():
    assert invoice_addresses({"customer_address": "Old road"})[:2] == ("Old road", "Old road")


def test_wrap_lines_limits_lines_and_width():
    lines = _wrap_lines("word " * 80, "Helvetica", 9, 200, 4)
    assert len(lines) == 4 and lines[-1].endswith("...")


def test_invoice_pdf_builds_with_both_addresses():
    invoice = {
        "invoice_no": "INV-1", "invoice_date": "2026-10-20", "customer_name": "Acme",
        "billing_address": "Shop 4\nNew Delhi", "shipping_address": "Warehouse 7, Okhla",
        "subtotal": 100, "total_cgst": 9, "total_sgst": 9, "total_igst": 0, "total_amount": 118,
    }
    items = [{"description": "Camera", "quantity": 1, "unit_price": 100, "gst_rate": 18, "cgst_amount": 9,
              "sgst_amount": 9, "igst_amount": 0, "line_total": 118}]
    assert build_invoice_pdf({"store_name": "Shop"}, invoice, items).startswith(b"%PDF")
