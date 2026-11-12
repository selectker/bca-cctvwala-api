"""Customer and supplier ledgers: every document and payment of one party in date order with a running balance.

Customer ledger (positive balance = the customer owes us):  invoices debit it; receipts, credit notes credit it; refunds
paid out and reversed receipts debit it again.
Supplier ledger (positive balance = we owe the supplier):  purchases credit it; payments and debit notes debit it; refunds
received and reversed payments credit it again.
The CREDIT_NOTE / DEBIT_NOTE payment rows a note books on its document are left out because the note itself is listed.
Cancelled documents (and the payments on them, which a cancel nets to zero) are left out.
"""
from datetime import date
from decimal import Decimal

<<<<<<< HEAD
=======
from app.utils.dates import today_local
>>>>>>> dc2fcd2 (Release v1.38.5)
from app.utils.gst import round2

ZERO = Decimal("0.00")


def _build(opening: Decimal, rows: list[dict], start: date | None, end: date | None, debit_increases: bool) -> dict:
    """rows: date, created_at, order, type, ref_no, particulars, doc_kind, doc_id, debit, credit (Decimals >= 0)."""
    sign = 1 if debit_increases else -1
    rows = sorted(rows, key=lambda r: (r["date"], r["created_at"], r["order"]))
    running = round2(opening)
    if start:
        for r in rows:
            if r["date"] < start:
                running += sign * (r["debit"] - r["credit"])
        rows = [r for r in rows if r["date"] >= start]
    if end:
        rows = [r for r in rows if r["date"] <= end]
    opening_at_start = round2(running)
    entries, total_debit, total_credit = [], ZERO, ZERO
    for r in rows:
        running += sign * (r["debit"] - r["credit"])
        total_debit += r["debit"]
        total_credit += r["credit"]
        entries.append({
            "date": r["date"], "type": r["type"], "ref_no": r["ref_no"], "particulars": r["particulars"],
            "doc_kind": r["doc_kind"], "doc_id": r["doc_id"], "debit": round2(r["debit"]), "credit": round2(r["credit"]),
            "balance": round2(running),
        })
    return {"opening_balance": opening_at_start, "entries": entries, "total_debit": round2(total_debit),
            "total_credit": round2(total_credit), "closing_balance": round2(running)}


def _payment_kind(amount: Decimal, has_note: bool, receipt: str, refund: str) -> str:
    if amount > 0:
        return receipt
    return refund if has_note else "Reversal"


async def customer_ledger(conn, customer_id: str, start: date | None, end: date | None) -> dict | None:
    party = await conn.fetchrow(
        "SELECT id, name, gstin, phone, address, state, is_walk_in, COALESCE(opening_balance, 0) AS opening_balance FROM customers WHERE id = $1",
        customer_id)
    if party is None:
        return None
    rows: list[dict] = []
    for r in await conn.fetch(
            "SELECT id, invoice_no, invoice_date, total_amount, created_at, buyer_name FROM invoices WHERE customer_id = $1 AND status = 'ACTIVE'",
            customer_id):
        rows.append({"date": r["invoice_date"], "created_at": r["created_at"], "order": 0, "type": "Invoice", "ref_no": r["invoice_no"],
                     "particulars": r["buyer_name"] or "Sale", "doc_kind": "invoice", "doc_id": str(r["id"]),
                     "debit": r["total_amount"], "credit": ZERO})
    for r in await conn.fetch(
            """SELECT p.id, p.amount, p.payment_mode, p.payment_date, p.reference_no, p.created_at, p.credit_note_id,
                      i.id AS invoice_id, i.invoice_no
               FROM payments p JOIN invoices i ON i.id = p.invoice_id
               WHERE i.customer_id = $1 AND i.status = 'ACTIVE' AND p.payment_mode <> 'CREDIT_NOTE'""", customer_id):
        amt = r["amount"]
        rows.append({"date": r["payment_date"], "created_at": r["created_at"], "order": 1,
                     "type": _payment_kind(amt, r["credit_note_id"] is not None, "Receipt", "Refund"),
                     "ref_no": r["reference_no"] or r["invoice_no"],
                     "particulars": f"{r['payment_mode'].replace('_', ' ').title()} against {r['invoice_no']}",
                     "doc_kind": "invoice", "doc_id": str(r["invoice_id"]),
                     "debit": -amt if amt < 0 else ZERO, "credit": amt if amt > 0 else ZERO})
    for r in await conn.fetch(
            """SELECT n.id, n.credit_note_no, n.note_date, n.total_amount, n.created_at, i.invoice_no
               FROM credit_notes n JOIN invoices i ON i.id = n.invoice_id WHERE n.customer_id = $1 AND n.status = 'ACTIVE'""", customer_id):
        rows.append({"date": r["note_date"], "created_at": r["created_at"], "order": 2, "type": "Credit note", "ref_no": r["credit_note_no"],
                     "particulars": f"Return against {r['invoice_no']}", "doc_kind": "credit_note", "doc_id": str(r["id"]),
                     "debit": ZERO, "credit": r["total_amount"]})
    out = _build(party["opening_balance"], rows, start, end, True)
    out["party"] = {k: party[k] for k in ("id", "name", "gstin", "phone", "address", "state")}
    out["party"]["id"] = str(party["id"])
    out["kind"] = "customer"
    return out


async def supplier_ledger(conn, supplier_id: str, start: date | None, end: date | None) -> dict | None:
    party = await conn.fetchrow(
        "SELECT id, name, gstin, phone, address, state, COALESCE(opening_balance, 0) AS opening_balance FROM suppliers WHERE id = $1",
        supplier_id)
    if party is None:
        return None
    rows: list[dict] = []
    for r in await conn.fetch(
            "SELECT id, purchase_no, purchase_date, total_amount, created_at, supplier_invoice_no FROM purchases WHERE supplier_id = $1 AND status = 'ACTIVE'",
            supplier_id):
        rows.append({"date": r["purchase_date"], "created_at": r["created_at"], "order": 0, "type": "Purchase", "ref_no": r["purchase_no"],
                     "particulars": f"Supplier invoice {r['supplier_invoice_no']}" if r["supplier_invoice_no"] else "Purchase",
                     "doc_kind": "purchase", "doc_id": str(r["id"]), "debit": ZERO, "credit": r["total_amount"]})
    for r in await conn.fetch(
            """SELECT p.id, p.amount, p.payment_mode, p.payment_date, p.reference_no, p.created_at, p.debit_note_id,
                      pu.id AS purchase_id, pu.purchase_no
               FROM payments p JOIN purchases pu ON pu.id = p.purchase_id
               WHERE pu.supplier_id = $1 AND pu.status = 'ACTIVE' AND p.payment_mode <> 'DEBIT_NOTE'""", supplier_id):
        amt = r["amount"]
        rows.append({"date": r["payment_date"], "created_at": r["created_at"], "order": 1,
                     "type": _payment_kind(amt, r["debit_note_id"] is not None, "Payment", "Refund received"),
                     "ref_no": r["reference_no"] or r["purchase_no"],
                     "particulars": f"{r['payment_mode'].replace('_', ' ').title()} against {r['purchase_no']}",
                     "doc_kind": "purchase", "doc_id": str(r["purchase_id"]),
                     "debit": amt if amt > 0 else ZERO, "credit": -amt if amt < 0 else ZERO})
    for r in await conn.fetch(
            """SELECT n.id, n.debit_note_no, n.note_date, n.total_amount, n.created_at, pu.purchase_no
               FROM debit_notes n JOIN purchases pu ON pu.id = n.purchase_id WHERE n.supplier_id = $1 AND n.status = 'ACTIVE'""", supplier_id):
        rows.append({"date": r["note_date"], "created_at": r["created_at"], "order": 2, "type": "Debit note", "ref_no": r["debit_note_no"],
                     "particulars": f"Return against {r['purchase_no']}", "doc_kind": "debit_note", "doc_id": str(r["id"]),
                     "debit": r["total_amount"], "credit": ZERO})
    out = _build(party["opening_balance"], rows, start, end, False)
    out["party"] = {k: party[k] for k in ("id", "name", "gstin", "phone", "address", "state")}
    out["party"]["id"] = str(party["id"])
    out["kind"] = "supplier"
    return out


def ledger_pdf(store: dict, data: dict, start: date | None, end: date | None) -> bytes:
    """A4 statement: shop name, party, period, the table (pages as needed) and the closing balance."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from app.utils.invoice_pdf import BAR, INK, L, R, _inr, _use_fonts

    reg, bold, sym = _use_fonts()
    is_customer = data["kind"] == "customer"
    title = "Customer Ledger" if is_customer else "Supplier Ledger"
    party = data["party"]
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, H = A4
    cols = [("Date", L, "l"), ("Type", L + 52, "l"), ("Ref", L + 108, "l"), ("Particulars", L + 178, "l"),
            ("Debit", L + 360, "r"), ("Credit", L + 430, "r"), ("Balance", R, "r")]

    def money(v):
        return _inr(v, sym, with_sym=False)

    def bal(v):
        suffix = ("Dr" if v >= 0 else "Cr") if is_customer else ("Cr" if v >= 0 else "Dr")
        return f"{money(abs(v))} {suffix}"

    def fit(text, font, size, width):
        text = str(text or "")
        while text and c.stringWidth(text, font, size) > width:
            text = text[:-1]
        return text

    def text(x, y, s, size=8.3, font=None, align="l"):
        c.setFont(font or reg, size)
        (c.drawRightString if align == "r" else c.drawString)(x, y, s)

    def header_row(y):
        c.setFillColorRGB(*BAR)
        c.rect(L, y - 4, R - L, 15, stroke=0, fill=1)
        c.setFillColorRGB(*INK)
        for name, x, al in cols:
            text(x + (0 if al == "l" else 0), y, name, 8.3, bold, al)
        return y - 16

    def first_page():
        c.setFillColorRGB(*INK)
        y = H - 50
        text(L, y, store.get("store_name") or "Store", 15, bold)
        if store.get("address"):
            y -= 13
            text(L, y, fit((store["address"]).replace("\n", ", "), reg, 8.3, R - L), 8.3)
        y -= 20
        text(L, y, title, 12, bold)
<<<<<<< HEAD
        period = f"{start.strftime('%d-%m-%Y') if start else 'Beginning'} to {(end or date.today()).strftime('%d-%m-%Y')}"
=======
        period = f"{start.strftime('%d-%m-%Y') if start else 'Beginning'} to {(end or today_local()).strftime('%d-%m-%Y')}"
>>>>>>> dc2fcd2 (Release v1.38.5)
        text(R, y, period, 8.3, reg, "r")
        y -= 15
        text(L, y, party["name"], 10, bold)
        for line in (party.get("gstin") and f"GSTIN: {party['gstin']}", party.get("phone") and f"Phone: {party['phone']}"):
            if line:
                y -= 11
                text(L, y, line, 8.3)
        return y - 16

    y = header_row(first_page())
    text(L + 178, y, "Opening balance", 8.3, bold)
    text(R, y, bal(data["opening_balance"]), 8.3, bold, "r")
    y -= 14
    for e in data["entries"]:
        if y < 70:
            c.showPage()
            c.setFillColorRGB(*INK)
            y = header_row(H - 50)
        text(cols[0][1], y, e["date"].strftime("%d-%m-%Y"))
        text(cols[1][1], y, fit(e["type"], reg, 8.3, 52))
        text(cols[2][1], y, fit(e["ref_no"], reg, 8.3, 68))
        text(cols[3][1], y, fit(e["particulars"], reg, 8.3, 176))
        if e["debit"]:
            text(cols[4][1], y, money(e["debit"]), 8.3, reg, "r")
        if e["credit"]:
            text(cols[5][1], y, money(e["credit"]), 8.3, reg, "r")
        text(R, y, bal(e["balance"]), 8.3, reg, "r")
        y -= 12.5
    if y < 90:
        c.showPage()
        y = H - 60
    c.setStrokeColorRGB(*INK)
    c.line(L, y + 6, R, y + 6)
    y -= 6
    text(L + 178, y, "Total", 8.3, bold)
    text(cols[4][1], y, money(data["total_debit"]), 8.3, bold, "r")
    text(cols[5][1], y, money(data["total_credit"]), 8.3, bold, "r")
    y -= 14
    text(L + 178, y, "Closing balance", 9, bold)
    text(R, y, bal(data["closing_balance"]), 9, bold, "r")
    y -= 22
    note = "Dr = the customer owes you." if is_customer else "Cr = you owe the supplier."
    text(L, y, f"{note} Cancelled documents are not listed. Computer generated statement.", 7.5)
    c.save()
    return buf.getvalue()
