"""Tax-invoice PDF in the boxed "Vyapar" layout: title, shop box, Bill To / Invoice Details, a full-height item table
(columns run to the footer, the Total row sits under them), HSN tax summary + totals + amount in words, bank details
with the pay-by-QR code, and the authorised-signatory cell.

`build_invoice_pdf` in app/utils/pdf.py calls `build()` here, so every caller keeps working unchanged."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.utils.gst import round2
from app.utils.gstr import STATE_NAMES, place_of_supply
from app.utils.upi import upi_payment_uri

# ------------------------------------------------------------------------------------------------ fonts
# Helvetica has no rupee sign, so the invoice uses DejaVu Sans Condensed (shipped in app/assets/fonts). If the
# files are missing the invoice still prints, in Helvetica with "Rs." instead of the symbol.
_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_fonts: tuple[str, str, str] | None = None


def _use_fonts() -> tuple[str, str, str]:
    """(regular, bold, rupee symbol)."""
    global _fonts
    if _fonts is None:
        try:
            pdfmetrics.registerFont(TTFont("InvSans", str(_FONT_DIR / "DejaVuSansCondensed.ttf")))
            pdfmetrics.registerFont(TTFont("InvSans-Bold", str(_FONT_DIR / "DejaVuSansCondensed-Bold.ttf")))
            _fonts = ("InvSans", "InvSans-Bold", "\u20b9")
        except Exception:
            _fonts = ("Helvetica", "Helvetica-Bold", "Rs.")
    return _fonts


# ------------------------------------------------------------------------------------------------ constants
L, R = 40.0, 555.0           # table / box edges (points)
BOTTOM = 46.0                # lowest y any box may reach (the "computer generated" note sits below it)
INK = (0.2, 0.21, 0.27)      # text and borders
BAR = (0.94, 0.94, 0.95)     # grey header bars
HEAD_BAR_H = 16.0
ROW_PAD = 3.5
LINE_H = 10.0
FS = 8.3                     # body text size


def _inr(value, sym: str, with_sym: bool = True) -> str:
    """Indian digit grouping with two decimals: 123456.5 -> '1,23,456.50' (with the rupee sign in front)."""
    v = round2(Decimal(str(value if value is not None else 0)))
    neg = v < 0
    whole, _, frac = f"{abs(v):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    text = f"{whole}.{frac}"
    if with_sym:
        text = f"{sym} {text}"
    return f"-{text}" if neg else text


def _dec(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def _qty(value) -> str:
    try:
        text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)
    return text or "0"


def _date(value) -> str:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    if isinstance(value, (date, datetime)):
        return value.strftime("%d-%m-%Y")
    return str(value or "")


def _state_label(code, name=None) -> str:
    """'07-Delhi' (the way the GST portal writes it); falls back to whatever was saved."""
    code = str(code or "").strip()
    if code:
        return place_of_supply(code)
    return str(name or "").strip()


class _Pen:
    """Small drawing helper so the layout code reads top-down (y values are PDF coordinates, origin bottom-left)."""

    def __init__(self, c, regular: str, bold: str):
        self.c, self.reg, self.bold = c, regular, bold

    def text(self, x, y, s, size=FS, bold=False, align="l", color=INK):
        c = self.c
        c.setFillColorRGB(*color)
        c.setFont(self.bold if bold else self.reg, size)
        {"l": c.drawString, "r": c.drawRightString, "c": c.drawCentredString}[align](x, y, str(s))

    def width(self, s, size=FS, bold=False) -> float:
        return stringWidth(str(s), self.bold if bold else self.reg, size)

    def kv(self, x, y, label, value, size=FS):
        """'Label: ' in regular type followed by the value in bold, on one line."""
        self.text(x, y, label, size)
        self.text(x + self.width(label, size) + 2, y, value, size, bold=True)

    def fit(self, s, size, max_w, bold=False) -> str:
        s = str(s)
        font = self.bold if bold else self.reg
        if stringWidth(s, font, size) <= max_w:
            return s
        while s and stringWidth(s + "...", font, size) > max_w:
            s = s[:-1]
        return s.rstrip() + "..."

    def wrap(self, text, size, max_w, max_lines, bold=False) -> list[str]:
        font = self.bold if bold else self.reg
        lines: list[str] = []
        for raw in str(text or "").replace("\r", "").split("\n"):
            current = ""
            for word in raw.split():
                trial = f"{current} {word}".strip()
                if current and stringWidth(trial, font, size) > max_w:
                    lines.append(current)
                    current = word
                else:
                    current = trial
            if current:
                lines.append(current)
        lines = [self.fit(x, size, max_w, bold) for x in lines]
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + "...", size, max_w, bold)
        return lines

    def rect(self, x0, y0, x1, y1, fill=None):
        c = self.c
        c.setStrokeColorRGB(*INK)
        c.setLineWidth(0.7)
        if fill:
            c.setFillColorRGB(*fill)
        c.rect(x0, y0, x1 - x0, y1 - y0, stroke=1, fill=1 if fill else 0)

    def hline(self, x0, x1, y):
        self.c.setStrokeColorRGB(*INK)
        self.c.setLineWidth(0.7)
        self.c.line(x0, y, x1, y)

    def vline(self, x, y0, y1):
        self.c.setStrokeColorRGB(*INK)
        self.c.setLineWidth(0.7)
        self.c.line(x, y0, x, y1)

    def bar(self, x0, x1, top, h, label, size=FS):
        """Grey header bar with a bold label."""
        self.rect(x0, top - h, x1, top, fill=BAR)
        self.text(x0 + 5, top - h + (h - size) / 2 + 1.5, label, size, bold=True)


# ------------------------------------------------------------------------------------------------ item rows
class _Row:
    def __init__(self, name_lines, hsn, qty, price, discount, disc_pct, gst_amt, gst_rate, amount):
        self.name_lines, self.hsn, self.qty, self.price = name_lines, hsn, qty, price
        self.discount, self.disc_pct, self.gst_amt, self.gst_rate, self.amount = discount, disc_pct, gst_amt, gst_rate, amount
        self.height = 0.0
        self.serial_lines: list[str] = []


SERIAL_FS, SERIAL_H, MUTED_SERIAL = 7.0, 8.6, INK


def _columns(has_discount: bool, show_gst: bool = True, sn_label: str = "#") -> list[tuple[str, float, float, str]]:
    """(key, left x, right x, header) -- the cells run edge to edge between L and R."""
    sn_w = 18 if sn_label == "#" else 32  # "S.No." needs more room than "#"; the extra width comes off the item column
    if has_discount:
        widths = [("sn", 18, sn_label), ("item", 150, "Item Name"), ("hsn", 54, "HSN/SAC"), ("qty", 44, "Quantity"),
                  ("price", 58, "Price/ Unit"), ("disc", 54, "Discount"), ("gst", 68, "GST"), ("amt", 69, "Amount")]
    else:
        widths = [("sn", 18, sn_label), ("item", 182, "Item Name"), ("hsn", 60, "HSN/SAC"), ("qty", 52, "Quantity"),
                  ("price", 62, "Price/ Unit"), ("gst", 71, "GST"), ("amt", 70, "Amount")]
    widths = [(k, w, h) for k, w, h in widths]
    if not show_gst:  # give the GST column's width to the item name
        gst_w = next(w for k, w, _ in widths if k == "gst")
        widths = [(k, w + gst_w if k == "item" else w, h) for k, w, h in widths if k != "gst"]
    widths = [(k, sn_w if k == "sn" else (w - (sn_w - 18) if k == "item" else w), h) for k, w, h in widths]
    cols, x = [], L
    for key, w, head in widths:
        cols.append((key, x, x + w, head))
        x += w
    cols[-1] = (cols[-1][0], cols[-1][1], R, cols[-1][3])  # absorb rounding so the table ends exactly at R
    return cols


def _prepare_rows(pen: _Pen, items: list[dict], cols, sym: str) -> list[_Row]:
    item_w = next(r - l for k, l, r, _ in cols if k == "item") - 10
    gst_w = next((r - l for k, l, r, _ in cols if k == "gst"), None)
    gst_w = gst_w - 8 if gst_w is not None else None
    rows = []
    for it in items:
        qty, price = _dec(it.get("quantity")), _dec(it.get("unit_price"))
        gross = round2(qty * price)
        disc = _dec(it.get("discount_amount"))
        pct = _dec(it.get("discount_percent"))
        gst_amt = _dec(it.get("cgst_amount")) + _dec(it.get("sgst_amount")) + _dec(it.get("igst_amount"))
        label = it.get("description") or it.get("product_name") or "-"
        hsn = str(it.get("hsn_code") or it.get("product_hsn_code") or "")[:10]
        # "(10%)" only when the whole discount is the line's own percent (not part of an invoice-level discount)
        shown_pct = pct if pct > 0 and disc == round2(gross * pct / 100) else Decimal(0)
        row = _Row(pen.wrap(label, FS, item_w, 3, bold=True), hsn, qty, price, disc, shown_pct, gst_amt, _dec(it.get("gst_rate")),
                   _dec(it.get("line_total")))
        gst_one_line = f"{_inr(gst_amt, sym)} ({row.gst_rate:.1f}%)"
        serials = [str(b) for b in (it.get("serials") or it.get("barcodes") or []) if b]
        if serials:  # unit serial numbers, printed small under the item name
            row.serial_lines = pen.wrap("S/N: " + ", ".join(serials), SERIAL_FS, item_w, 14)
        gst_lines = 1 if gst_w is None or pen.width(gst_one_line) <= gst_w else 2
        lines = max(len(row.name_lines), gst_lines, 2 if shown_pct > 0 else 1)
        row.height = lines * LINE_H + 2 * ROW_PAD
        if row.serial_lines:
            row.height = max(row.height, len(row.name_lines) * LINE_H + 2 * ROW_PAD) + len(row.serial_lines) * SERIAL_H + 1
        rows.append(row)
    return rows


def _draw_row(pen: _Pen, row: _Row, top: float, cols, sym: str) -> None:
    pos = {k: (l, r) for k, l, r, _ in cols}
    y = top - ROW_PAD - 7.2

    def right(key, s, yy=y, size=FS):
        pen.text(pos[key][1] - 4, yy, s, size, align="r")

    pen.text(pos["sn"][0] + 4, y, getattr(row, "number", ""), FS)
    for i, line in enumerate(row.name_lines):
        pen.text(pos["item"][0] + 4, y - i * LINE_H, line, FS, bold=True)
    for j, line in enumerate(row.serial_lines):
        pen.text(pos["item"][0] + 4, y - len(row.name_lines) * LINE_H + 1 - j * SERIAL_H, line, SERIAL_FS, color=MUTED_SERIAL)
    pen.text(pos["hsn"][0] + 4, y, row.hsn, FS)
    right("qty", _qty(row.qty))
    right("price", _inr(row.price, sym))
    if "disc" in pos:
        right("disc", _inr(row.discount, sym) if row.discount > 0 else "")
        if row.disc_pct > 0:
            right("disc", f"({_qty(row.disc_pct)}%)", y - LINE_H, 7)
    if "gst" in pos:
        one_line = f"{_inr(row.gst_amt, sym)} ({row.gst_rate:.1f}%)"
        gst_w = pos["gst"][1] - pos["gst"][0] - 8
        if pen.width(one_line) <= gst_w:
            right("gst", one_line)
        else:
            right("gst", _inr(row.gst_amt, sym))
            right("gst", f"({row.gst_rate:.1f}%)", y - LINE_H, 7)
    right("amt", _inr(row.amount, sym))


# ------------------------------------------------------------------------------------------------ tax summary
def _tax_groups(items: list[dict]) -> list[dict]:
    """One line per (HSN/SAC, GST rate): taxable value and the CGST / SGST / IGST on it."""
    groups: dict[tuple[str, Decimal], dict] = {}
    for it in items:
        hsn = str(it.get("hsn_code") or it.get("product_hsn_code") or "")[:10]
        rate = _dec(it.get("gst_rate"))
        g = groups.setdefault((hsn, rate), {"hsn": hsn, "rate": rate, "taxable": Decimal(0), "cgst": Decimal(0),
                                            "sgst": Decimal(0), "igst": Decimal(0)})
        g["taxable"] += round2(_dec(it.get("quantity")) * _dec(it.get("unit_price")) - _dec(it.get("discount_amount")))
        g["cgst"] += _dec(it.get("cgst_amount"))
        g["sgst"] += _dec(it.get("sgst_amount"))
        g["igst"] += _dec(it.get("igst_amount"))
    return [groups[k] for k in sorted(groups)]


_SUMMARY_W = 360.0   # width of the tax-summary panel; the totals panel takes the rest (L + 360 .. R)
_SUM_ROW_H = 14.0


def _summary_columns(igst_mode: bool) -> list[tuple[str, float]]:
    if igst_mode:
        return [("hsn", 74), ("taxable", 87), ("rate", 49), ("amt", 50), ("tax", 100)]
    return [("hsn", 52), ("taxable", 76), ("crate", 32), ("camt", 48), ("srate", 32), ("samt", 48), ("tax", 72)]


def _totals_rows(invoice: dict, items: list[dict], sym: str, balance: Decimal) -> list[tuple[str, str, bool]]:
    """(label, value, bold) rows above the 'amount in words' bar."""
    sub = round2(sum((_dec(i.get("line_total")) for i in items), Decimal(0))) if items else _dec(invoice.get("total_amount"))
    rows = [("Sub Total", _inr(sub, sym), False)]
    if _dec(invoice.get("round_off")) != 0:
        rows.append(("Round Off", _inr(invoice.get("round_off"), sym), False))
    rows.append(("Total", _inr(invoice.get("total_amount"), sym), True))
    saved = _dec(invoice.get("total_discount"))
    if saved > 0:
        rows.append(("You Saved", _inr(saved, sym), False))
        inv_disc = _dec(invoice.get("invoice_discount_amount"))
        if inv_disc > 0:
            pct = _dec(invoice.get("invoice_discount_percent"))
            rows.append((f"incl. invoice disc.{f' ({_qty(pct)}%)' if pct > 0 else ''}", _inr(inv_disc, sym), False))
    return rows


# ------------------------------------------------------------------------------------------------ pieces of the page
@dataclass
class Spec:
    """What differs between the documents that share the invoice's item-table layout (invoice, quotation, purchase).
    The defaults print a tax invoice exactly as before."""
    title: str = "Tax Invoice"
    number_label: str = "Invoice No"      # "Invoice No: X (continued)" on the following pages
    party_label: str = "Bill To:"
    details_label: str = "Invoice Details:"
    details: list | None = None           # right-hand rows [(kind, value)]; None = the invoice's own rows
    footer_note: str = "This is a computer-generated invoice."
    words_label: str = "Invoice Amount In Words :"
    show_payment: bool = True             # the Received / Balance rows under the totals
    paid_label: str = "Received"
    show_bank: bool = True                # bank details (and the pay-by-QR code, which also needs a balance owed)
    notes: str | None = None              # printed in the bottom-left cell
    show_signature: bool = True           # the store's signature image + "Authorized Signatory" caption (off for internal records)
    show_gst_col: bool = True             # per-item GST column (the tax invoice drops it; the HSN tax summary still shows the tax)
    sn_label: str = "#"                   # header of the serial-number column
    corner_label: str | None = None       # bold label printed at the top right of every page (e.g. the copy type)


def _draw_title(pen: _Pen, top: float, title: str = "Tax Invoice") -> float:
    pen.text(297.5, top - 14, title, 16, bold=True, align="c")
    return top - 26


FOOT_GREY = (0.45, 0.45, 0.5)


def begin_page(pen: _Pen, c, store: dict, title: str, number_label: str, number, page_no: int, logo: bytes | None,
               cancelled: bool = False, draw_stamp=None, corner_label: str | None = None) -> float:
    """Start a page the way every document does: stamp when cancelled, centred title, then on page 1 the shop box and on
    later pages a right-aligned "<No label>: <number>  (continued)". Returns the y where the next box starts."""
    width, height = A4
    if cancelled and draw_stamp:
        draw_stamp(c, width, height)
    top = _draw_title(pen, height - 36, title)
    if corner_label:  # e.g. "ORIGINAL FOR RECIPIENT", top right, on the title's line
        pen.text(R, height - 50, corner_label, 8.5, bold=True, align="r")
    if page_no == 1:
        return _draw_shop_box(pen, c, store, top, logo)
    pen.text(R, top - 2, f"{number_label}: {number or ''}  (continued)", 8, align="r")
    return top - 8


def end_page_continued(pen: _Pen) -> None:
    pen.text(R, BOTTOM - 10, "Continued on next page...", 7.5, align="r", color=FOOT_GREY)


def end_last_page(pen: _Pen, note: str) -> None:
    pen.text(297.5, 30, note, 7.5, align="c", color=FOOT_GREY)


def _draw_shop_box(pen: _Pen, c, store: dict, top: float, logo: bytes | None) -> float:
    img, logo_w, logo_h = None, 0.0, 0.0
    if logo:
        try:
            img = ImageReader(BytesIO(logo))
            iw, ih = img.getSize()
            scale = min(104 / iw, 62 / ih)
            logo_w, logo_h = iw * scale, ih * scale
        except Exception:
            img = None
    text_x = L + 10 + (logo_w + 14 if img else 0)
    room = R - 10 - text_x
    address = pen.wrap((store.get("address") or "").replace("\n", ", "), 8.3, room, 2)
    box_h = max(14 + 18 + 12 * len(address) + 26, logo_h + 18)
    pen.rect(L, top - box_h, R, top)
    if img:
        try:
            c.drawImage(img, L + 10, top - 9 - logo_h, width=logo_w, height=logo_h, mask="auto")
        except Exception:
            pass
    y = top - 21
    pen.text(text_x, y, pen.fit(store.get("store_name") or "Store", 14.5, room, True), 14.5, bold=True)
    y -= 15
    for line in address:
        pen.text(text_x, y, line, 8.3)
        y -= 12
    mid = text_x + max(150.0, room * 0.45)
    y -= 1
    if store.get("phone"):
        pen.kv(text_x, y, "Phone:", store["phone"])
    if store.get("email"):
        pen.kv(mid, y, "Email:", pen.fit(store["email"], FS, R - 10 - mid - 30))
    y -= 13
    if store.get("gstin"):
        pen.kv(text_x, y, "GSTIN:", store["gstin"])
    state = _state_label(store.get("state_code"), store.get("state"))
    if state:
        pen.kv(mid, y, "State:", state)
    return top - box_h


def _draw_party_box(pen: _Pen, invoice: dict, ticket_nos: list[str] | None, top: float, owed: bool,
                    spec: "Spec | None" = None) -> float:
    from app.utils.pdf import invoice_addresses  # (imported here: pdf.py imports this module)

    split = L + 309.0
    left_w = split - L - 12
    billing, shipping, same = invoice_addresses(invoice)
    bill_lines = pen.wrap(billing, FS, left_w, 4)

    left = [("name", pen.fit(invoice.get("customer_name") or "", 9.5, left_w, True))]
    left += [("addr", ln) for ln in bill_lines]
    if not same:
        left.append(("gap", ""))
        left.append(("shiplabel", "Ship To:"))
        left += [("addr", ln) for ln in pen.wrap(shipping, FS, left_w, 3)]
    contact = [("kv", ("Contact No:", invoice.get("customer_phone")))] if invoice.get("customer_phone") else []
    cust_state = _state_label(invoice.get("customer_state_code") or invoice.get("place_of_supply_state_code"), invoice.get("customer_state"))
    if cust_state:
        contact.append(("kv", ("State:", cust_state)))
    gstin = invoice.get("customer_gstin")

    right = [("kv", ("No:", str(invoice.get("invoice_no") or ""))), ("kv", ("Date:", _date(invoice.get("invoice_date")))),
             ("kv", ("Place of Supply:", place_of_supply(invoice.get("place_of_supply_state_code")) or "-"))]
    if invoice.get("due_date") and owed:
        right.append(("kv", ("Due Date:", _date(invoice["due_date"]))))
    if ticket_nos:
        right.append(("plain", "Service ref:"))
        right += [("bold", ln) for ln in pen.wrap(", ".join(ticket_nos), FS, R - split - 16, 2)]

    if spec is not None and spec.details is not None:
        right = list(spec.details)

    left_h = 8 + 11.5 * len(left) + (3 + 11.5 * max(len(contact), 2 if gstin else 0) if (contact or gstin) else 0)
    right_h = 8 + 12 * len(right)
    body_h = max(left_h, right_h, 40) + 4

    pen.bar(L, split, top, HEAD_BAR_H, spec.party_label if spec else "Bill To:")
    pen.bar(split, R, top, HEAD_BAR_H, spec.details_label if spec else "Invoice Details:")
    body_top = top - HEAD_BAR_H
    pen.rect(L, body_top - body_h, split, body_top)
    pen.rect(split, body_top - body_h, R, body_top)

    y = body_top - 12
    for kind, value in left:
        if kind == "name":
            pen.text(L + 5, y, value, 9.5, bold=True)
        elif kind == "shiplabel":
            pen.text(L + 5, y, value, FS, bold=True)
        elif kind == "addr":
            pen.text(L + 5, y, value, FS)
        y -= 11.5 if kind != "gap" else 4
    y -= 3
    cy = y
    for _, (label, value) in contact:
        pen.kv(L + 5, cy, label, value)
        cy -= 11.5
    if gstin:
        gx = L + 5 + 150
        pen.text(gx, y, "GSTIN Number:", FS)
        pen.text(gx, y - 11.5, gstin, FS, bold=True)

    y = body_top - 12
    for kind, value in right:
        if kind == "kv":
            pen.kv(split + 6, y, *value)
        elif kind == "bold":
            pen.text(split + 6, y, value, FS, bold=True)
        else:
            pen.text(split + 6, y, value, FS)
        y -= 12
    return body_top - body_h


def _draw_table_header(pen: _Pen, cols, top: float) -> float:
    h = 18.0
    pen.rect(L, top - h, R, top, fill=BAR)
    for key, l, r, head in cols:
        if key == "sn":
            pen.text(l + 4, top - 12, head, FS, bold=True)
        elif key in ("item", "hsn"):
            pen.text(l + 4, top - 12, head, FS, bold=True)
        else:
            pen.text(r - 4, top - 12, head, FS, bold=True, align="r")
    return top - h


def _draw_frame(pen: _Pen, cols, body_top: float, body_bottom: float, header_top: float) -> None:
    """Outer border and the column rules running from the header bar down to body_bottom."""
    pen.rect(L, body_bottom, R, header_top)
    for _, l, _, _ in cols[1:]:
        pen.vline(l, header_top, body_bottom)


SIGN_SPLIT = L + 257.0  # the signatory cell keeps its half-width even when the left cell is empty


def _left_cell(pen: _Pen, store: dict, spec: Spec, show_qr: bool):
    """(label, bank rows, note lines) for the bottom-left cell; label is None when the cell is not printed."""
    bank = []
    if spec.show_bank and store.get("bank_name"):
        bank = [(lab, val) for lab, val in (("Name:", store.get("bank_name")), ("Account No.:", store.get("bank_account_no")),
                                             ("IFSC code:", store.get("bank_ifsc")),
                                             ("Account Holder's Name:", store.get("store_name"))) if val]
    notes = pen.wrap(spec.notes, FS, SIGN_SPLIT - L - 12, 5) if spec.notes else []
    if bank or show_qr:
        return "Bank Details:", bank, notes
    if notes:
        return "Notes:", [], notes
    return None, [], notes


def _left_cell_height(label, bank, notes, show_qr: bool) -> float:
    if label is None:
        return 0.0
    h = 12 + 11.5 * len(bank) + 6
    if notes:
        h += (4 + 11.5 if label == "Bank Details:" else 0) + 11.5 * len(notes)
    return max(h, 12 + _QR + 8) if show_qr else h


def _draw_footer(pen: _Pen, c, store: dict, invoice: dict, items: list[dict], cols, top: float, sym: str,
                 signature: bytes | None, balance: Decimal, show_qr: bool, igst_mode: bool, geometry: dict,
                 spec: Spec | None = None) -> None:
    """Total row, tax summary + totals, bank / signature row. `top` is the y where the Total row starts."""
    spec = spec or Spec()
    # ---- Total row
    total_h = 18.0
    pen.rect(L, top - total_h, R, top)
    for _, l, _, _ in cols[1:]:
        pen.vline(l, top, top - total_h)
    pos = {k: (l, r) for k, l, r, _ in cols}
    ty = top - 12
    pen.text(pos["item"][0] + 4, ty, "Total", FS, bold=True)
    pen.text(pos["qty"][1] - 4, ty, _qty(sum((_dec(i.get("quantity")) for i in items), Decimal(0))), FS, bold=True, align="r")
    if "disc" in pos:
        d = sum((_dec(i.get("discount_amount")) for i in items), Decimal(0))
        if d > 0:
            pen.text(pos["disc"][1] - 4, ty, _inr(d, sym), FS, bold=True, align="r")
    if "gst" in pos:
        pen.text(pos["gst"][1] - 4, ty, _inr(sum((_dec(i.get("cgst_amount")) + _dec(i.get("sgst_amount")) + _dec(i.get("igst_amount"))
                                                  for i in items), Decimal(0)), sym), FS, bold=True, align="r")
    pen.text(pos["amt"][1] - 4, ty, _inr(sum((_dec(i.get("line_total")) for i in items), Decimal(0)), sym), FS, bold=True, align="r")
    y = top - total_h

    # ---- Tax summary (left) and totals (right)
    groups = geometry["groups"]
    totals = geometry["totals"]
    block_h = geometry["summary_h"]
    split = L + _SUMMARY_W
    pen.rect(L, y - block_h, split, y)
    pen.rect(split, y - block_h, R, y)
    pen.bar(L, split, y, HEAD_BAR_H, "Tax Summary:")

    sc = _summary_columns(igst_mode)
    xs, x = [], L
    for key, w in sc:
        xs.append((key, x, x + w))
        x += w
    xs[-1] = (xs[-1][0], xs[-1][1], split)
    col = {k: (l, r) for k, l, r in xs}
    head_top = y - HEAD_BAR_H
    head_h = 2 * _SUM_ROW_H
    pen.rect(L, head_top - head_h, split, head_top, fill=BAR)
    for key, l, _ in xs[1:]:
        if key in ("rate", "amt", "crate", "camt", "srate", "samt"):
            continue
        pen.vline(l, head_top, head_top - head_h)
    mid_y = head_top - _SUM_ROW_H
    spans = ([("rate", "amt", "IGST")] if igst_mode else [("crate", "camt", "CGST"), ("srate", "samt", "SGST")])
    for a, b, label in spans:
        pen.vline(col[a][0], head_top, head_top - head_h)
        pen.hline(col[a][0], col[b][1], mid_y)
        pen.text((col[a][0] + col[b][1]) / 2, head_top - 10, label, FS, bold=True, align="c")
        pen.text(col[a][1] - 3, mid_y - 10, "Rate(%)", 6.6, bold=True, align="r")
        pen.text(col[b][1] - 3, mid_y - 10, f"Amt({sym})", 6.6, bold=True, align="r")
        pen.vline(col[b][0], mid_y, head_top - head_h)
    pen.text(col["hsn"][0] + (col["hsn"][1] - col["hsn"][0]) / 2, head_top - 17, "HSN/SAC", 7.4, bold=True, align="c")
    pen.text(col["taxable"][1] - 4, head_top - 17, f"Taxable Amount({sym})", 6.8, bold=True, align="r")
    pen.text(col["tax"][1] - 4, head_top - 17, f"Total Tax({sym})", 6.8, bold=True, align="r")

    ry = head_top - head_h
    body_bottom = y - block_h

    def draw_group_row(g, bold=False):
        nonlocal ry
        base = ry - 10
        pen.text(col["hsn"][0] + 4, base, g["hsn"], FS, bold=bold) if not bold else pen.text(col["hsn"][0] + (col["hsn"][1] - col["hsn"][0]) / 2, base, "TOTAL", FS, bold=True, align="c")
        pen.text(col["taxable"][1] - 4, base, f"{round2(g['taxable']):.2f}", FS, bold=bold, align="r")
        if igst_mode:
            if not bold:
                pen.text(col["rate"][1] - 4, base, f"{g['rate']:.1f}", FS, align="r")
            pen.text(col["amt"][1] - 4, base, f"{round2(g['igst']):.2f}", FS, bold=bold, align="r")
            tax = g["igst"]
        else:
            if not bold:
                pen.text(col["crate"][1] - 4, base, f"{g['rate'] / 2:.1f}", FS, align="r")
                pen.text(col["srate"][1] - 4, base, f"{g['rate'] / 2:.1f}", FS, align="r")
            pen.text(col["camt"][1] - 4, base, f"{round2(g['cgst']):.2f}", FS, bold=bold, align="r")
            pen.text(col["samt"][1] - 4, base, f"{round2(g['sgst']):.2f}", FS, bold=bold, align="r")
            tax = g["cgst"] + g["sgst"]
        pen.text(col["tax"][1] - 4, base, f"{round2(tax):.2f}", FS, bold=bold, align="r")
        ry -= _SUM_ROW_H

    for g in groups:
        draw_group_row(g)
    pen.hline(L, split, ry)
    total = {"hsn": "", "taxable": sum((g["taxable"] for g in groups), Decimal(0)), "rate": Decimal(0),
             "cgst": sum((g["cgst"] for g in groups), Decimal(0)), "sgst": sum((g["sgst"] for g in groups), Decimal(0)),
             "igst": sum((g["igst"] for g in groups), Decimal(0))}
    draw_group_row(total, bold=True)
    # the rules between the summary columns, from the header down through the TOTAL row
    for key, l, _ in xs[1:]:
        pen.vline(l, head_top - head_h, ry)

    # totals panel
    ty = y
    for label, value, bold in totals:
        ty -= 16
        pen.text(split + 6, ty + 5, label, FS, bold=bold)
        pen.text(split + 80, ty + 5, ":", FS, bold=bold)
        pen.text(R - 5, ty + 5, value, FS, bold=bold, align="r")
        if bold:
            pen.hline(split, R, ty + 16)
            pen.hline(split, R, ty)
    pen.bar(split, R, ty, HEAD_BAR_H, spec.words_label)
    ty -= HEAD_BAR_H
    from app.utils.pdf import amount_in_words  # (imported here: pdf.py imports this module)
    words = pen.wrap(amount_in_words(invoice.get("total_amount")), FS, R - split - 12, 3)
    for ln in words:
        ty -= 11
        pen.text(split + 6, ty + 1, ln, FS)
    ty -= 6
    pen.hline(split, R, ty)
    pay_rows = ((spec.paid_label, _inr(invoice.get("paid_amount"), sym), False), ("Balance", _inr(balance, sym), True)) if spec.show_payment else ()
    for label, value, bold in pay_rows:
        ty -= 16
        pen.text(split + 6, ty + 5, label, FS, bold=bold)
        pen.text(split + 80, ty + 5, ":", FS, bold=bold)
        pen.text(R - 5, ty + 5, value, FS, bold=bold, align="r")

    # ---- Bank details / notes (left) and signatory (right)
    y = body_bottom
    left_label, bank_rows, note_lines = _left_cell(pen, store, spec, show_qr)
    left_present = left_label is not None
    sign_split = SIGN_SPLIT
    body_h = geometry["sign_h"]
    if left_present:
        pen.bar(L, sign_split, y, HEAD_BAR_H, left_label)
        pen.rect(L, y - HEAD_BAR_H - body_h, sign_split, y - HEAD_BAR_H)
    pen.bar(sign_split, R, y, HEAD_BAR_H, f"For {pen.fit(store.get('store_name') or 'Store', FS, R - sign_split - 50, True)}:")
    pen.rect(sign_split, y - HEAD_BAR_H - body_h, R, y - HEAD_BAR_H)
    inner_top = y - HEAD_BAR_H

    by = inner_top - 12
    for label, value in bank_rows:
        pen.kv(L + 5, by, label, pen.fit(value, FS, (sign_split - L) - 12 - (80 if show_qr else 0) - pen.width(label) - 6))
        by -= 11.5
    if note_lines:
        if left_label == "Bank Details:":
            by -= 4
            pen.text(L + 5, by, "Notes:", FS, bold=True)
            by -= 11.5
        for ln in note_lines:
            pen.text(L + 5, by, ln, FS)
            by -= 11.5
    if show_qr:
        _draw_qr(pen, c, store, invoice, balance, sym, sign_split - 8, inner_top)

    # signature image centred in the cell, "Authorized Signatory" under it
    cx = (sign_split + R) / 2
    if signature and spec.show_signature:
        try:
            img = ImageReader(BytesIO(signature))
            iw, ih = img.getSize()
            scale = min(130 / iw, 44 / ih)
            w, h = iw * scale, ih * scale
            c.drawImage(img, cx - w / 2, inner_top - body_h + 20, width=w, height=h, mask="auto")
        except Exception:
            pass
    if spec.show_signature:
        pen.text(cx, inner_top - body_h + 7, "Authorized Signatory", FS, align="c")


_QR = 64.0


def _draw_qr(pen: _Pen, c, store: dict, invoice: dict, balance: Decimal, sym: str, right_x: float, top: float) -> None:
    """'Scan to pay' QR for the balance, tucked into the right of the bank-details cell."""
    uri = upi_payment_uri(store["upi_id"], store.get("store_name") or "", balance, str(invoice.get("invoice_no") or ""))
    widget = QrCodeWidget(uri)
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(_QR, _QR, transform=[_QR / (x1 - x0), 0, 0, _QR / (y1 - y0), 0, 0])
    drawing.add(widget)
    x = right_x - _QR
    pen.text(x + _QR / 2, top - 9, "Scan to pay", 7.4, bold=True, align="c")
    renderPDF.draw(drawing, c, x, top - 12 - _QR)


# ------------------------------------------------------------------------------------------------ entry point
def build(store: dict, invoice: dict, items: list[dict], logo: bytes | None = None, signature: bytes | None = None,
          ticket_nos: list[str] | None = None, draw_stamp=None) -> bytes:
    """The tax invoice."""
    spec = Spec(show_gst_col=False, sn_label="S.No.", corner_label="ORIGINAL FOR RECIPIENT")
    return build_document(store, invoice, items, logo, signature, ticket_nos, draw_stamp, spec)


def build_document(store: dict, invoice: dict, items: list[dict], logo: bytes | None, signature: bytes | None,
                   ticket_nos: list[str] | None, draw_stamp, spec: Spec) -> bytes:
    """Invoice, quotation or purchase record in the boxed layout. `invoice` carries the document's own fields under the
    invoice's names (invoice_no, invoice_date, customer_*, total_amount, ...) -- see build_quotation_pdf / build_purchase_pdf."""
    regular, bold, sym = _use_fonts()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    pen = _Pen(c, regular, bold)

    cancelled = invoice.get("status") == "CANCELLED"
    paid = _dec(invoice.get("paid_amount"))
    balance = _dec(invoice.get("total_amount")) - paid
    owed = (not cancelled) and balance > 0 and spec.show_payment
    show_qr = bool(store.get("upi_id")) and owed and spec.show_bank
    has_discount = any(_dec(i.get("discount_amount")) > 0 for i in items)
    igst_mode = bool(invoice.get("is_interstate")) or _dec(invoice.get("total_igst")) > 0
    if igst_mode and _dec(invoice.get("total_cgst")) > 0:
        igst_mode = False  # a stored invoice never mixes them; trust the amounts

    cols = _columns(has_discount, spec.show_gst_col, spec.sn_label)
    rows = _prepare_rows(pen, items, cols, sym)
    for n, row in enumerate(rows, 1):
        row.number = str(n)

    groups = _tax_groups(items)
    totals = _totals_rows(invoice, items, sym, balance)
    # footer geometry (the same on every page that carries it, so the table can be sized to leave room for it)
    summary_left = HEAD_BAR_H + 2 * _SUM_ROW_H + _SUM_ROW_H * (len(groups) + 1) + 4
    words_n = len(pen.wrap(_amount_words(invoice), FS, R - (L + _SUMMARY_W) - 12, 3))
    pay_h = 32 if spec.show_payment else 0
    summary_right = 16 * len(totals) + HEAD_BAR_H + 11 * words_n + 6 + pay_h + 4
    summary_h = max(summary_left, summary_right)
    left_label, bank_rows, note_lines = _left_cell(pen, store, spec, show_qr)
    sign_h = 78.0 if (show_qr or bank_rows) else 64.0
    sign_h = max(sign_h, _left_cell_height(left_label, bank_rows, note_lines, show_qr))
    geometry = {"groups": groups, "totals": totals, "summary_h": summary_h, "sign_h": sign_h}
    total_row_h = 18.0
    footer_h = total_row_h + summary_h + HEAD_BAR_H + sign_h
    final_bottom = BOTTOM + footer_h  # where the item area ends on the last page

    number = invoice.get("invoice_no")
    idx, page_no = 0, 0
    while True:
        page_no += 1
        top = begin_page(pen, c, store, spec.title, spec.number_label, number, page_no, logo, cancelled, draw_stamp,
                         spec.corner_label)
        if page_no == 1:
            top = _draw_party_box(pen, {**invoice}, ticket_nos, top, owed, spec)
        header_top = top
        body_top = _draw_table_header(pen, cols, header_top)

        remaining = sum(r.height for r in rows[idx:])
        if remaining <= body_top - final_bottom:
            last = True
            body_bottom = final_bottom
            take = rows[idx:]
        else:
            last = False
            body_bottom = BOTTOM
            room = body_top - body_bottom - 14
            take, used = [], 0.0
            for r in rows[idx:]:
                if used + r.height > room and take:
                    break
                take.append(r)
                used += r.height
            if len(take) == len(rows) - idx and len(take) > 1:
                take.pop()  # never leave the last page with an empty item area
        _draw_frame(pen, cols, body_top, body_bottom, header_top)
        y = body_top
        for r in take:
            _draw_row(pen, r, y, cols, sym)
            y -= r.height
        idx += len(take)
        if last:
            _draw_footer(pen, c, store, invoice, items, cols, body_bottom, sym, signature, balance, show_qr, igst_mode, geometry, spec)
            end_last_page(pen, spec.footer_note)
            c.showPage()
            break
        end_page_continued(pen)
        c.showPage()

    c.save()
    return buffer.getvalue()


def _amount_words(invoice: dict) -> str:
    from app.utils.pdf import amount_in_words
    return amount_in_words(invoice.get("total_amount"))
