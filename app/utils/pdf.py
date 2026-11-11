"""Every printable document, in the invoice's page pattern.

* Item-table documents (invoice, quotation, purchase record) are drawn by app/utils/invoice_pdf.py.
* The others (job card, warranty card, AMC contract, payment receipt / voucher) are drawn on the page framework in
  app/utils/doc_pdf.py, which reproduces the invoice's title, shop box, grey-barred boxes, continuation lines, signatory
  cell and footer note.
"""
from datetime import date, datetime
from decimal import Decimal

from reportlab.pdfbase.pdfmetrics import stringWidth

from app.utils.doc_pdf import CELL_SPLIT, Doc, Item
from app.utils.gst import round2
from app.utils.gstr import place_of_supply
from app.utils.invoice_pdf import FS, Spec, _date, _inr, build, build_document


def _fmt_date(value) -> str:
    """dd-mm-yyyy, the way the invoice prints dates."""
    if isinstance(value, (date, datetime)):
        return value.strftime("%d-%m-%Y")
    return _date(value) if value else ""


def _d(value) -> Decimal:
    """Decimal for the sums printed on a document (so a balance never shows float noise)."""
    return Decimal(str(value if value is not None else 0))


def _num(value) -> str:
    """Quantity as a short string: 2.00 -> "2", 1.50 -> "1.5"."""
    try:
        text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)
    return text or "0"


def _draw_cancelled_stamp(c, width: float, height: float) -> None:
    """Big diagonal red CANCELLED mark so a cancelled document can't be mistaken for a live one."""
    c.saveState()
    c.setFillColorRGB(0.8, 0.1, 0.1)
    c.setFillAlpha(0.18)
    c.setFont("Helvetica-Bold", 90)
    c.translate(width / 2, height / 2)
    c.rotate(35)
    c.drawCentredString(0, 0, "CANCELLED")
    c.restoreState()


def _fit(text: str, font: str, size: float, max_width: float) -> str:
    """Text cut (with an ellipsis) so it fits max_width points on one line."""
    if stringWidth(text, font, size) <= max_width:
        return text
    while text and stringWidth(text + "...", font, size) > max_width:
        text = text[:-1]
    return text.rstrip() + "..."


def _wrap_lines(text: str | None, font: str, size: float, max_width: float, max_lines: int) -> list[str]:
    """Address text broken into at most max_lines lines that fit max_width points. Line breaks the user typed
    are kept; the last line ends with an ellipsis if there was more."""
    lines: list[str] = []
    for raw in str(text or "").replace("\r", "").split("\n"):
        words = raw.split()
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and stringWidth(trial, font, size) > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
    lines = [_fit(line, font, size, max_width) for line in lines]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(lines[-1] + "...", font, size, max_width)
    return lines


def invoice_addresses(invoice: dict) -> tuple[str, str, bool]:
    """(billing, shipping, same) as printed on the invoice. Invoices made before v1.24.0 have no saved copy, so
    they fall back to the customer's address. Shipping empty or equal to billing means \"same as billing\"."""
    billing = str(invoice.get("billing_address") or invoice.get("customer_address") or "").strip()
    shipping = str(invoice.get("shipping_address") or "").strip()
    same = not shipping or " ".join(shipping.split()) == " ".join(billing.split())
    return billing, (billing if same else shipping), same




_DESC_WIDTH = 172  # points between the item column and the HSN/SAC column


def _item_hsn(item: dict) -> str:
    """HSN / SAC for a line: the code stored on the line, else the product's current one (old invoices), else blank."""
    return str(item.get("hsn_code") or item.get("product_hsn_code") or "")[:10]


def build_invoice_pdf(store: dict, invoice: dict, items: list[dict], logo: bytes | None = None,
                      signature: bytes | None = None, ticket_nos: list[str] | None = None) -> bytes:
    """Tax invoice in the boxed layout (see app/utils/invoice_pdf.py)."""
    return build(store, invoice, items, logo, signature, ticket_nos, draw_stamp=_draw_cancelled_stamp)


def _doc(store: dict, title: str, number_label: str, number, note: str, logo, cancelled: bool = False) -> Doc:
    return Doc(store, title, number_label, number, note, logo, cancelled, _draw_cancelled_stamp)


def _split_gst(items: list[dict], interstate: bool) -> list[dict]:
    """Quotation / purchase lines only store one gst_amount; give each the CGST + SGST (or IGST) split the item table prints."""
    out = []
    for item in items:
        row = dict(item)
        if all(row.get(k) is not None for k in ("cgst_amount", "sgst_amount", "igst_amount")):
            out.append(row)
            continue
        gst = _d(row.get("gst_amount"))
        if interstate:
            cgst, sgst, igst = Decimal(0), Decimal(0), gst
        else:
            cgst = round2(gst / 2)
            sgst, igst = gst - cgst, Decimal(0)
        row.update(cgst_amount=cgst, sgst_amount=sgst, igst_amount=igst)
        out.append(row)
    return out


def _item_document(store, doc_no, doc_date, party: dict, total_amount, rows: list[dict], interstate: bool, spec: Spec,
                   extra: dict, logo, signature) -> bytes:
    """Hand a quotation / purchase to the invoice's item-table layout under the invoice's field names."""
    lines_total = sum((_d(r.get("line_total")) for r in rows), Decimal(0))
    total = _d(total_amount)
    doc = {
        "invoice_no": doc_no, "invoice_date": doc_date, "total_amount": total,
        "round_off": round2(total - lines_total) if rows else Decimal(0),
        "is_interstate": interstate,
        "total_igst": sum((_d(r.get("igst_amount")) for r in rows), Decimal(0)),
        "total_cgst": sum((_d(r.get("cgst_amount")) for r in rows), Decimal(0)),
        "customer_name": party.get("name"), "customer_address": party.get("address"), "customer_gstin": party.get("gstin"),
        "customer_phone": party.get("phone"), "customer_state": party.get("state"), "customer_state_code": party.get("state_code"),
        **extra,
    }
    return build_document(store, doc, rows, logo, signature, None, _draw_cancelled_stamp, spec)


def build_quotation_pdf(store: dict, quotation: dict, items: list[dict], logo: bytes | None = None,
                        signature: bytes | None = None) -> bytes:
    """quotation: row + customer_name/customer_address/customer_gstin/customer_phone/customer_state(_code) + total_cgst/sgst/igst
    (the GST split worked out from the customer's state). items: quotation_items + product_name."""
    interstate = _d(quotation.get("total_igst")) > 0
    rows = _split_gst(items, interstate)
    details = [("kv", ("No:", str(quotation.get("quotation_no") or ""))), ("kv", ("Date:", _fmt_date(quotation.get("quotation_date"))))]
    if quotation.get("valid_till"):
        details.append(("kv", ("Valid Till:", _fmt_date(quotation["valid_till"]))))
    if quotation.get("customer_state_code"):
        details.append(("kv", ("Place of Supply:", place_of_supply(quotation["customer_state_code"]) or "-")))
    details.append(("kv", ("Status:", str(quotation.get("status") or "").title())))
    spec = Spec(title="Quotation", number_label="Quotation No", party_label="Quotation For:", details_label="Quotation Details:",
                details=details, words_label="Quotation Amount In Words :",
                footer_note="This is a computer-generated quotation and is not a tax invoice.",
                show_payment=False, show_bank=True, notes=quotation.get("notes"))
    party = {"name": quotation.get("customer_name"), "address": quotation.get("customer_address"), "gstin": quotation.get("customer_gstin"),
             "phone": quotation.get("customer_phone"), "state": quotation.get("customer_state"), "state_code": quotation.get("customer_state_code")}
    extra = {"total_discount": quotation.get("total_discount"), "invoice_discount_amount": quotation.get("invoice_discount_amount"),
             "invoice_discount_percent": quotation.get("invoice_discount_percent"), "paid_amount": 0, "status": quotation.get("status"),
             "place_of_supply_state_code": quotation.get("customer_state_code")}
    return _item_document(store, quotation.get("quotation_no"), quotation.get("quotation_date"), party, quotation["total_amount"], rows,
                          interstate, spec, extra, logo, signature)


def build_purchase_pdf(store: dict, purchase: dict, items: list[dict], logo: bytes | None = None,
                       signature: bytes | None = None) -> bytes:
    """purchase: row + supplier_name/supplier_address/supplier_gstin/supplier_phone/supplier_state(_code). items: purchase_items + product_name."""
    shop_state, supplier_state = str(store.get("state_code") or ""), str(purchase.get("supplier_state_code") or "")
    interstate = bool(shop_state and supplier_state and shop_state != supplier_state)
    rows = _split_gst(items, interstate)
    details = [("kv", ("No:", str(purchase.get("purchase_no") or ""))), ("kv", ("Date:", _fmt_date(purchase.get("purchase_date"))))]
    if purchase.get("supplier_invoice_no"):
        details.append(("kv", ("Supplier Inv No:", str(purchase["supplier_invoice_no"]))))
    if purchase.get("payment_status"):
        details.append(("kv", ("Payment:", str(purchase["payment_status"]).title())))
    spec = Spec(title="Purchase Record", number_label="Purchase No", party_label="Supplier:", details_label="Purchase Details:",
                details=details, words_label="Amount In Words :", footer_note="Internal purchase record.", show_payment=True, paid_label="Paid", show_bank=False,
                notes=purchase.get("notes"), show_signature=False)
    party = {"name": purchase.get("supplier_name"), "address": purchase.get("supplier_address"), "gstin": purchase.get("supplier_gstin"),
             "phone": purchase.get("supplier_phone"), "state": purchase.get("supplier_state"), "state_code": purchase.get("supplier_state_code")}
    extra = {"paid_amount": purchase.get("paid_amount"), "status": purchase.get("status")}
    return _item_document(store, purchase.get("purchase_no"), purchase.get("purchase_date"), party, purchase["total_amount"], rows,
                          interstate, spec, extra, logo, signature)


def build_credit_note_pdf(store: dict, note: dict, items: list[dict], logo: bytes | None = None,
                          signature: bytes | None = None) -> bytes:
    """note: credit_notes row + customer_* fields + invoice_no / invoice_date. items: credit_note_items (+ product_name)."""
    interstate = bool(note.get("is_interstate"))
    rows = []
    for it in items:
        row = dict(it)
        row.setdefault("discount_percent", 0)
        if row.get("barcodes"):
            row["serials"] = list(row["barcodes"])
        rows.append(row)
    details = [("kv", ("No:", str(note.get("credit_note_no") or ""))), ("kv", ("Date:", _fmt_date(note.get("note_date")))),
               ("kv", ("Against Invoice:", str(note.get("invoice_no") or ""))),
               ("kv", ("Invoice Date:", _fmt_date(note.get("invoice_date"))))]
    if note.get("place_of_supply_state_code"):
        details.append(("kv", ("Place of Supply:", place_of_supply(note["place_of_supply_state_code"]) or "-")))
    if note.get("reason"):
        details.append(("kv", ("Reason:", str(note["reason"])[:40])))
    spec = Spec(title="Credit Note", number_label="Credit Note No", party_label="Credit To:", details_label="Credit Note Details:",
                details=details, words_label="Credit Amount In Words :",
                footer_note="Credit note issued against the invoice shown above.",
                show_payment=False, show_bank=False, notes=note.get("notes"), show_signature=True)
    party = {"name": note.get("customer_name"), "address": note.get("customer_address"), "gstin": note.get("customer_gstin"),
             "phone": note.get("customer_phone"), "state": note.get("customer_state"), "state_code": note.get("customer_state_code")}
    extra = {"paid_amount": 0, "status": note.get("status"), "round_off": note.get("round_off"),
             "total_igst": note.get("total_igst"), "total_cgst": note.get("total_cgst"),
             "place_of_supply_state_code": note.get("place_of_supply_state_code")}
    return _item_document(store, note.get("credit_note_no"), note.get("note_date"), party, note["total_amount"], rows,
                          interstate, spec, extra, logo, signature)


def build_debit_note_pdf(store: dict, note: dict, items: list[dict], logo: bytes | None = None,
                         signature: bytes | None = None) -> bytes:
    """note: debit_notes row + supplier_* fields + purchase_no / supplier_invoice_no. items: debit_note_items + product_name."""
    shop_state, supplier_state = str(store.get("state_code") or ""), str(note.get("supplier_state_code") or "")
    interstate = bool(shop_state and supplier_state and shop_state != supplier_state)
    rows = []
    for row in _split_gst(items, interstate):
        row.setdefault("discount_percent", 0)
        rows.append(row)
    details = [("kv", ("No:", str(note.get("debit_note_no") or ""))), ("kv", ("Date:", _fmt_date(note.get("note_date")))),
               ("kv", ("Against Purchase:", str(note.get("purchase_no") or "")))]
    if note.get("supplier_invoice_no"):
        details.append(("kv", ("Supplier Inv No:", str(note["supplier_invoice_no"]))))
    if note.get("reason"):
        details.append(("kv", ("Reason:", str(note["reason"])[:40])))
    spec = Spec(title="Debit Note", number_label="Debit Note No", party_label="Debit To (Supplier):", details_label="Debit Note Details:",
                details=details, words_label="Debit Amount In Words :", footer_note="Debit note for goods returned to the supplier.",
                show_payment=False, show_bank=False, notes=note.get("notes"), show_signature=True)
    party = {"name": note.get("supplier_name"), "address": note.get("supplier_address"), "gstin": note.get("supplier_gstin"),
             "phone": note.get("supplier_phone"), "state": note.get("supplier_state"), "state_code": note.get("supplier_state_code")}
    extra = {"paid_amount": 0, "status": note.get("status")}
    return _item_document(store, note.get("debit_note_no"), note.get("note_date"), party, note["total_amount"], rows,
                          interstate, spec, extra, logo, signature)


_TICKET_STATUS = {"OPEN": "Open", "RESCHEDULED": "Rescheduled", "ATTENDED": "Attended", "CLOSED": "Closed", "CANCELLED": "Cancelled"}
_TICKET_TYPE = {"IN_WARRANTY": "In warranty", "PAID": "Paid service", "AMC": "AMC covered"}
_EVENT_LABEL = {"CREATED": "Raised", "UPDATED": "Edited", "RESCHEDULED": "Rescheduled", "ATTENDED": "Attended",
                "CLOSED": "Closed", "CANCELLED": "Cancelled", "REOPENED": "Sent back", "ADDENDUM": "Added later"}




def build_ticket_pdf(store: dict, ticket: dict, events: list[dict], show_charges: bool, logo: bytes | None = None,
                     signoff: dict | None = None, signature_png: bytes | None = None) -> bytes:
    """One-page-or-more job card for a service ticket: who, where, what, the visit timeline, and boxes for the
    technician's work notes and the customer's / technician's signatures. ticket: the ticket row + customer_name /
    customer_phone / assigned_to_name / warranty_* / amc_contract_no / invoice_no. events: timeline rows (event_type, note,
    scheduled_date, created_at, actor_name). show_charges is False for field staff, who never see the money.
    signoff: the customer's sign-off for the latest visit (customer_name, acknowledged, obtained, not_obtained_reason,
    signed_at) with signature_png its image; the customer's signature box is filled in from it, and stays blank without one."""
    doc = _doc(store, "Service Job Card", "Ticket No", ticket.get("ticket_no"), "Job card - not a tax invoice.", logo,
               ticket.get("status") == "CANCELLED")
    sym = doc.sym  # the rupee sign (or "Rs." when the font is missing)

    left = [("name", ticket.get("customer_name") or "")]
    if ticket.get("customer_phone"):
        left.append(("kv", ("Contact No:", str(ticket["customer_phone"]))))
    right = [
        ("kv", ("No:", str(ticket.get("ticket_no") or ""))),
        ("kv", ("Type:", _TICKET_TYPE.get(ticket.get("service_type"), str(ticket.get("service_type") or "")))),
        ("kv", ("Status:", _TICKET_STATUS.get(ticket.get("status"), str(ticket.get("status") or "")))),
        ("kv", ("Raised On:", _fmt_date(ticket.get("created_at")))),
    ]
    if ticket.get("scheduled_date"):
        right.append(("kv", ("Visit Date:", _fmt_date(ticket["scheduled_date"]))))
    if ticket.get("amc_contract_no"):
        right.append(("kv", ("AMC Contract:", str(ticket["amc_contract_no"])[:22])))
    if ticket.get("assigned_to_name"):
        right.append(("kv", ("Assigned To:", str(ticket["assigned_to_name"])[:22])))
    doc.details_boxes("Customer:", left, "Ticket Details:", right)

    doc.box("Site Address:", doc.paragraphs(ticket.get("site_address") or "-"))

    problem = doc.paragraphs(ticket.get("title") or "-", size=9.3, bold=True)
    if ticket.get("description"):
        problem += [Item(kind="gap")] + doc.paragraphs(ticket["description"])
    doc.box("Problem:", problem)

    if ticket.get("warranty_barcode"):
        unit = []
        if ticket.get("warranty_product_name"):
            unit.append(Item(str(ticket["warranty_product_name"]), bold=True, size=9.3))
        unit.append(doc.kv("Barcode:", ticket["warranty_barcode"], 100))
        if ticket.get("warranty_valid_until"):
            unit.append(doc.kv("Customer Warranty Until:", _fmt_date(ticket["warranty_valid_until"]), 100))
        doc.box("Unit:", unit)

    if show_charges and ticket.get("service_type") == "PAID":
        charges = []
        charge = ticket.get("charge_amount")
        if charge is None:
            charges.append(doc.kv("Estimate:", "Not set", 100))
        else:
            gst = _d(ticket.get("gst_rate"))
            total = round2(_d(charge) + _d(charge) * gst / Decimal("100"))
            charges.append(doc.kv("Estimate:", f"{_inr(charge, sym)} + GST {_num(gst)}% = {_inr(total, sym)}", 100))
        if ticket.get("invoice_no"):
            charges.append(doc.kv("Billed On Invoice:", ticket["invoice_no"], 100))
        doc.box("Charges:", charges)

    if events:
        history = []
        for ev in events:
            who = f" by {ev['actor_name']}" if ev.get("actor_name") else ""
            label = _EVENT_LABEL.get(ev.get("event_type"), str(ev.get("event_type") or ""))
            extra = f" (new date {_fmt_date(ev['scheduled_date'])})" if ev.get("scheduled_date") else ""
            history.append(Item(f"{_fmt_date(ev.get('created_at'))}  {label}{who}{extra}", bold=True))
            if ev.get("note"):
                history += doc.paragraphs(ev["note"], x=12)
        doc.box("History:", history)

    # Blank work-done box and sign-off cells, so the card can be printed, filled in on site, and signed.
    doc.blank_box("Work Done / Parts Used:", 70)
    obtained = bool(signoff and signoff.get("obtained"))
    captions = ["Customer Signature"]
    image, text = None, None
    if obtained:
        # The customer's sign-off: the signature image above the caption, and the name and date in it. A customer who
        # acknowledged without drawing gets "Acknowledged by customer" instead.
        when = f", {_fmt_date(signoff['signed_at'])}" if signoff.get("signed_at") else ""
        captions = [f"Customer signature - {signoff.get('customer_name') or ''}{when}"]
        if signature_png:
            image = signature_png
        else:
            text = "Acknowledged by customer"
    elif signoff and signoff.get("not_obtained_reason"):
        captions.append(f"Not obtained: {signoff['not_obtained_reason']}")
    doc.sign_row("Customer:", store.get("store_name") or "", None, image, text, captions, right_label="Technician:",
                 right_caption="Technician Signature")
    return doc.finish()


WARRANTY_TERMS = (
    "The warranty covers manufacturing defects only, for the period shown above, from the date of sale.",
    "Please keep this card and the tax invoice; both are needed to claim service under warranty.",
    "The warranty does not cover physical or liquid damage, burns, power surges, lightning, misuse, "
    "or repair / opening of the product by anyone other than us or an authorised service centre.",
    "The warranty is void if the serial number / barcode is removed, tampered with or does not match this card.",
    "Consumables, cables, adaptors and software are not covered unless stated on the invoice.",
    "A defective unit may be repaired or replaced at our discretion; replacement continues the original warranty period.",
)


def build_warranty_card_pdf(store: dict, unit: dict, logo: bytes | None = None, signature: bytes | None = None) -> bytes:
    """Customer-facing warranty certificate for one sold unit. unit: the part_warranties row + product_name / product_sku /
    customer_name / customer_phone / customer_address / customer_gstin / invoice_no / invoice_date / replaces_barcode."""
    doc = _doc(store, "Warranty Card", "Barcode", unit.get("barcode"),
               "Warranty card - not a tax invoice. Valid only with the original invoice.", logo)

    left = [("name", unit.get("customer_name") or "")]
    if unit.get("customer_phone"):
        left.append(("kv", ("Contact No:", str(unit["customer_phone"]))))
    if unit.get("customer_address"):
        left += [("addr", ln) for ln in doc.pen.wrap(str(unit["customer_address"]).replace("\n", ", "), FS, CELL_SPLIT - 40 - 12, 3)]
    if unit.get("customer_gstin"):
        left.append(("kv", ("GSTIN:", str(unit["customer_gstin"]))))
    invoice = f"{unit.get('invoice_no') or '-'}" + (f"  ({_fmt_date(unit['invoice_date'])})" if unit.get("invoice_date") else "")
    months = unit.get("customer_warranty_months")
    right = [("kv", ("Invoice:", invoice)),
             ("kv", ("Warranty Starts:", _fmt_date(unit["sale_date"]) if unit.get("sale_date") else "-"))]
    if months:
        right.append(("kv", ("Period:", f"{months} month{'s' if months != 1 else ''}")))
    right.append(("kv", ("Warranty Ends:", _fmt_date(unit.get("customer_warranty_valid_until")) or "-")))
    doc.details_boxes("Customer:", left, "Warranty Details:", right)

    product = [Item(str(unit.get("product_name") or "-"), bold=True, size=9.3)]
    if unit.get("product_sku"):
        product.append(doc.kv("SKU:", unit["product_sku"], 100))
    product.append(doc.kv("Serial / Barcode:", unit.get("barcode") or "-", 100))
    if unit.get("replaces_barcode"):
        product.append(doc.kv("Replacement For Unit:", unit["replaces_barcode"], 100))
    doc.box("Product:", product)

    terms = []
    for n, term in enumerate(WARRANTY_TERMS, 1):
        terms += doc.paragraphs(term, prefix=f"{n}.", hang=14)
    doc.box("Terms And Conditions:", terms)
    if unit.get("notes"):
        doc.box("Note:", doc.paragraphs(str(unit["notes"]).replace("\n", " "), max_lines=6))

    doc.sign_row(None, store.get("store_name") or "", signature)
    return doc.finish()


def build_amc_pdf(store: dict, contract: dict, visits: list[dict], logo: bytes | None = None, signature: bytes | None = None) -> bytes:
    """Customer-facing summary of an AMC contract: who, where, the term, the price, what is covered, the planned visits
    and signature cells. contract: the amc_contracts row + customer_name / customer_phone / customer_address /
    customer_gstin / renewed_from_no. visits: rows with scheduled_date, status, completed_date."""
    doc = _doc(store, "Annual Maintenance Contract", "Contract No", contract.get("contract_no"),
               "Annual maintenance contract - not a tax invoice.", logo, contract.get("status") == "CANCELLED")
    sym = doc.sym

    left = [("name", contract.get("customer_name") or "")]
    if contract.get("customer_phone"):
        left.append(("kv", ("Contact No:", str(contract["customer_phone"]))))
    if contract.get("customer_address"):
        left += [("addr", ln) for ln in doc.pen.wrap(str(contract["customer_address"]).replace("\n", ", "), FS, CELL_SPLIT - 40 - 12, 3)]
    if contract.get("customer_gstin"):
        left.append(("kv", ("GSTIN:", str(contract["customer_gstin"]))))
    freq_names = {"MONTHLY": "Monthly", "QUARTERLY": "Quarterly", "HALF_YEARLY": "Half-yearly", "YEARLY": "Yearly"}
    right = [("kv", ("No:", str(contract.get("contract_no") or "-"))), ("kv", ("Status:", str(contract.get("status") or "-").title()))]
    if contract.get("renewed_from_no"):
        right.append(("kv", ("Renewal Of:", str(contract["renewed_from_no"]))))
    right += [("kv", ("Start Date:", _fmt_date(contract.get("start_date")))), ("kv", ("End Date:", _fmt_date(contract.get("end_date")))),
              ("kv", ("Service Visits:", f"{freq_names.get(contract.get('visit_frequency'), contract.get('visit_frequency') or '-')} ({len(visits)})"))]
    doc.details_boxes("Customer:", left, "Contract Details:", right)

    if contract.get("site_address"):
        doc.box("Site Address:", doc.paragraphs(str(contract["site_address"]).replace("\n", ", ")))

    amount = _d(contract.get("amount"))
    rate = _d(contract.get("gst_rate"))
    gst = round2(amount * rate / 100)
    doc.box("Contract Value:", [
        doc.kv("Contract Amount:", f"{_inr(amount, sym)}  (excl. GST)", 120),
        doc.kv(f"GST @ {_num(rate)}%:", _inr(gst, sym), 120),
        doc.kv("Total Payable:", _inr(amount + gst, sym), 120, bold=True),
    ])

    if contract.get("coverage_notes"):
        doc.box("What Is Covered:", doc.paragraphs(contract["coverage_notes"]))

    if visits:
        shown = visits[:36]
        cells = []
        for n, v in enumerate(shown, 1):
            note = {"COMPLETED": "done", "MISSED": "missed"}.get(v.get("status"), "")
            cells.append(f"{n}.  {_fmt_date(v.get('scheduled_date'))}" + (f" ({note})" if note else ""))
        rows = [Item(kind="cols", cells=cells[i:i + 3]) for i in range(0, len(cells), 3)]
        if len(visits) > len(shown):
            rows.append(Item(f"... and {len(visits) - len(shown)} more"))
        doc.box("Visit Schedule:", rows)

    doc.sign_row("Customer:", store.get("store_name") or "", signature, left_captions=["Customer Signature"])
    return doc.finish()


# ---------------------------------------------------------------------------------------------- payment receipt
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _words_below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def amount_in_words(amount) -> str:
    """Rupee amount in words, Indian grouping: 125000.50 -> "Rupees One Lakh Twenty Five Thousand and Fifty Paise Only"."""
    value = round2(_d(amount))
    rupees, paise = int(value), int((value - int(value)) * 100)
    if rupees == 0 and paise == 0:
        return "Rupees Zero Only"
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        if rupees >= size:
            parts.append(_words_below_1000(rupees // size) + " " + name)
            rupees %= size
    if rupees:
        parts.append(_words_below_1000(rupees))
    text = "Rupees " + " ".join(parts) if parts else "Rupees Zero"
    if paise:
        text += f" and {_words_below_1000(paise)} Paise"
    return text + " Only"


def build_receipt_pdf(store: dict, kind: str, doc: dict, payment: dict, party: dict,
                      logo: bytes | None = None, signature: bytes | None = None) -> bytes:
    """One page: a receipt for a payment received on an invoice (kind "invoice"), or a voucher for a payment made on a
    purchase (kind "purchase"). `party` is {"name", "gstin"} of the customer / supplier. The balance shown is the
    document's balance on the day it is printed."""
    sale = kind == "invoice"
    page = _doc(store, "Payment Receipt" if sale else "Payment Voucher", "Receipt No" if sale else "Voucher No",
                ("R-" if sale else "V-") + str(payment["id"])[:8].upper(),
                "This is a computer-generated receipt." if sale else "This is a computer-generated voucher.", logo,
                doc.get("status") == "CANCELLED")
    sym = page.sym

    left = [("name", party.get("name") or "-")]
    if party.get("gstin"):
        left.append(("kv", ("GSTIN:", str(party["gstin"]))))
    right = [("kv", ("No:", ("R-" if sale else "V-") + str(payment["id"])[:8].upper())),
             ("kv", ("Date:", _fmt_date(payment.get("payment_date")))),
             ("kv", ("Payment Mode:", str(payment.get("payment_mode") or "-"))),
             ("kv", ("Reference No:", str(payment.get("reference_no") or "-")))]
    page.details_boxes("Received From:" if sale else "Paid To:", left, "Receipt Details:" if sale else "Voucher Details:", right)

    amount = _d(payment["amount"])
    page.box("Amount Received:" if sale else "Amount Paid:",
             [Item(_inr(amount, sym), bold=True, size=15), Item(kind="gap")] + page.paragraphs(amount_in_words(amount), max_lines=2))

    number = doc["invoice_no" if sale else "purchase_no"]
    date_ = doc["invoice_date" if sale else "purchase_date"]
    balance = _d(doc["total_amount"]) - _d(doc.get("paid_amount"))
    page.box("Against Invoice:" if sale else "Against Purchase:", [
        page.kv("Document:", f"{number}  dated {_fmt_date(date_)}", 150),
        page.kv("Document Total:", _inr(doc["total_amount"], sym), 150),
        page.kv("Paid So Far:", _inr(doc.get("paid_amount"), sym), 150),
        page.kv(f"Balance (as on {_fmt_date(date.today())}):", _inr(balance, sym), 150, bold=True),
    ])
    if payment.get("notes"):
        page.box("Notes:", page.paragraphs(payment["notes"], max_lines=4))

    page.sign_row(None, store.get("store_name") or "", signature)
    return page.finish()
