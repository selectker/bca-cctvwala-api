"""Page framework for the documents that are not item tables (job card, warranty card, AMC contract, payment receipt /
voucher). It reproduces the invoice's page pattern -- centred title, boxed shop header with logo, grey-barred boxes that
run edge to edge, "Continued on next page..." / "<No>: X (continued)" on long documents, the "For <shop>:" signatory cell
and the grey computer-generated note -- so every PDF the app prints looks like it came from the same shop.

Typical use (see app/utils/pdf.py):

    doc = Doc(store, "Warranty Card", "Barcode", number, "Warranty card - not a tax invoice.", logo)
    doc.details_boxes("Customer:", left_rows, "Warranty Details:", right_rows)
    doc.box("Terms And Conditions:", doc.paragraphs(...))
    doc.sign_row(None, store_name, signature)
    return doc.finish()
"""
from dataclasses import dataclass, field
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.utils.invoice_pdf import (BOTTOM, FS, HEAD_BAR_H, L, R, SIGN_SPLIT, _Pen, _use_fonts, begin_page,
                                   end_last_page, end_page_continued)

PAD = 4.0                 # space above the first and below the last line inside a box
LINE = 11.5               # height of one text line at the body size
CELL_SPLIT = L + 309.0    # the same split the invoice uses between Bill To and Invoice Details


@dataclass
class Item:
    """One line inside a box. kind: text | kv | cols | gap."""
    text: str = ""
    kind: str = "text"
    bold: bool = False
    size: float = FS
    x: float = 0.0                      # indent from the box's left padding
    prefix: str = ""                    # e.g. "1." drawn at x, with the text at x + hang
    hang: float = 0.0
    value: str = ""                     # kv: the value (bold) printed after the label
    label_w: float = 110.0              # kv: x of the value
    cells: list = field(default_factory=list)   # cols: texts, one per column

    @property
    def height(self) -> float:
        if self.kind == "gap":
            return 5.0
        return max(LINE, self.size + 3.5)


class Doc:
    def __init__(self, store: dict, title: str, number_label: str, number, footer_note: str,
                 logo: bytes | None = None, cancelled: bool = False, draw_stamp=None):
        regular, bold, sym = _use_fonts()
        self.store, self.title, self.number_label, self.number = store, title, number_label, number
        self.footer_note, self.logo, self.cancelled, self.draw_stamp = footer_note, logo, cancelled, draw_stamp
        self.buffer = BytesIO()
        self.c = canvas.Canvas(self.buffer, pagesize=A4)
        self.pen = _Pen(self.c, regular, bold)
        self.sym = sym
        self.page_no = 0
        self.top = 0.0
        self._start_page()

    # ------------------------------------------------------------------------------------------ pages
    def _start_page(self) -> None:
        self.page_no += 1
        self.top = begin_page(self.pen, self.c, self.store, self.title, self.number_label, self.number, self.page_no,
                              self.logo, self.cancelled, self.draw_stamp)

    def next_page(self) -> None:
        end_page_continued(self.pen)
        self.c.showPage()
        self._start_page()

    def ensure(self, height: float) -> None:
        """Move to a new page when `height` points would run into the bottom margin."""
        if self.top - height < BOTTOM:
            self.next_page()

    def finish(self) -> bytes:
        end_last_page(self.pen, self.footer_note)
        self.c.showPage()
        self.c.save()
        return self.buffer.getvalue()

    # ------------------------------------------------------------------------------------------ text helpers
    def paragraphs(self, text, size: float = FS, bold: bool = False, x: float = 0.0, prefix: str = "", hang: float = 0.0,
                   max_lines: int = 200) -> list[Item]:
        """Wrapped text as box items (user line breaks kept)."""
        width = (R - L) - 12 - x - hang
        lines = self.pen.wrap(text, size, width, max_lines, bold=bold)
        items = []
        for n, line in enumerate(lines):
            items.append(Item(line, bold=bold, size=size, x=x, prefix=prefix if n == 0 else "", hang=hang))
        return items

    def kv(self, label: str, value, label_w: float = 110.0, bold: bool = False) -> Item:
        return Item(label, kind="kv", value=str(value), label_w=label_w, bold=bold)

    # ------------------------------------------------------------------------------------------ boxes
    def _draw_item(self, it: Item, cursor: float, left: float, right: float) -> None:
        pen = self.pen
        base = cursor - (it.height - 3.0)
        x = left + it.x
        if it.kind == "gap":
            return
        if it.kind == "kv":
            pen.text(x, base, it.text, it.size, bold=it.bold)
            pen.text(x + it.label_w, base, pen.fit(it.value, it.size, right - (x + it.label_w) - 6, True), it.size, bold=True)
            return
        if it.kind == "cols":
            step = (right - left) / max(len(it.cells), 1)
            for n, cell in enumerate(it.cells):
                pen.text(left + n * step, base, cell, it.size, bold=it.bold)
            return
        if it.prefix:
            pen.text(x, base, it.prefix, it.size, bold=it.bold)
        pen.text(x + it.hang, base, it.text, it.size, bold=it.bold)

    def box(self, label: str, items: list[Item], min_body: float = 0.0) -> None:
        """Full-width box under a grey bar. A long box carries on after a page break under the same bar."""
        i, first = 0, True
        items = items or [Item("-")]
        while True:
            if self.top - BOTTOM < HEAD_BAR_H + 2 * PAD + items[i].height + 2:
                self.next_page()
            avail = self.top - BOTTOM - HEAD_BAR_H - 2 * PAD
            used, take = 0.0, []
            for it in items[i:]:
                if used + it.height > avail and take:
                    break
                take.append(it)
                used += it.height
            body_h = max(used + 2 * PAD, min_body if i + len(take) >= len(items) else 0.0)
            self.pen.bar(L, R, self.top, HEAD_BAR_H, label if first else f"{label.rstrip(':')} (continued):")
            body_top = self.top - HEAD_BAR_H
            self.pen.rect(L, body_top - body_h, R, body_top)
            cursor = body_top - PAD
            for it in take:
                self._draw_item(it, cursor, L + 6, R - 6)
                cursor -= it.height
            self.top = body_top - body_h
            i += len(take)
            first = False
            if i >= len(items):
                return
            self.next_page()

    def blank_box(self, label: str, height: float) -> None:
        self.ensure(HEAD_BAR_H + height)
        self.pen.bar(L, R, self.top, HEAD_BAR_H, label)
        self.pen.rect(L, self.top - HEAD_BAR_H - height, R, self.top - HEAD_BAR_H)
        self.top -= HEAD_BAR_H + height

    def details_boxes(self, left_label: str, left: list, right_label: str, right: list) -> None:
        """Two boxes side by side (like Bill To / Invoice Details). Rows are (kind, value): name, addr, bold, plain, gap,
        kv (value is (label, value))."""
        pen = self.pen
        split = CELL_SPLIT
        left_w = split - L - 12

        def height(rows, step):
            return PAD + sum(4.0 if kind == "gap" else step for kind, _ in rows) + PAD

        body_h = max(height(left, LINE), height(right, 12.0), 40.0)
        self.ensure(HEAD_BAR_H + body_h)
        pen.bar(L, split, self.top, HEAD_BAR_H, left_label)
        pen.bar(split, R, self.top, HEAD_BAR_H, right_label)
        body_top = self.top - HEAD_BAR_H
        pen.rect(L, body_top - body_h, split, body_top)
        pen.rect(split, body_top - body_h, R, body_top)

        def paint(rows, x, step, width):
            y = body_top - 12
            for kind, value in rows:
                if kind == "name":
                    pen.text(x, y, pen.fit(value, 9.5, width, True), 9.5, bold=True)
                elif kind == "kv":
                    label, val = value
                    pen.kv(x, y, label, pen.fit(val, FS, width - pen.width(label) - 8, True))
                elif kind == "bold":
                    pen.text(x, y, value, FS, bold=True)
                elif kind in ("addr", "plain"):
                    pen.text(x, y, value, FS)
                y -= 4.0 if kind == "gap" else step

        paint(left, L + 5, LINE, left_w)
        paint(right, split + 6, 12.0, R - split - 12)
        self.top = body_top - body_h

    # ------------------------------------------------------------------------------------------ signatures
    def sign_row(self, left_label: str | None, store_name: str, signature: bytes | None = None, left_image: bytes | None = None,
                 left_text: str | None = None, left_captions: list[str] | None = None, right_label: str | None = None,
                 right_caption: str = "Authorized Signatory", height: float = 64.0) -> None:
        """Bottom row like the invoice's: an optional left cell (e.g. Customer / Technician) and the "For <shop>:" signatory
        cell, each with its bar. The right cell keeps its half width when there is no left cell."""
        pen, c = self.pen, self.c
        captions = left_captions or []
        height = max(height, 36 + 10 * len(captions) + (24 if (left_image or left_text) else 0))
        self.ensure(HEAD_BAR_H + height)
        y = self.top
        inner_top = y - HEAD_BAR_H
        inner_bottom = inner_top - height
        if left_label:
            pen.bar(L, SIGN_SPLIT, y, HEAD_BAR_H, left_label)
            pen.rect(L, inner_bottom, SIGN_SPLIT, inner_top)
            cx = (L + SIGN_SPLIT) / 2
            self._signature(cx, inner_bottom, inner_top, left_image, left_text, captions, SIGN_SPLIT - L - 12)
        label = right_label or f"For {pen.fit(store_name or 'Store', FS, R - SIGN_SPLIT - 50, True)}:"
        pen.bar(SIGN_SPLIT, R, y, HEAD_BAR_H, label)
        pen.rect(SIGN_SPLIT, inner_bottom, R, inner_top)
        self._signature((SIGN_SPLIT + R) / 2, inner_bottom, inner_top, signature, None, [right_caption], R - SIGN_SPLIT - 12)
        self.top = inner_bottom

    def _signature(self, cx: float, bottom: float, top: float, image: bytes | None, text: str | None,
                   captions: list[str], width: float) -> None:
        pen = self.pen
        y = bottom + 7
        for line in captions:  # first caption lowest
            pen.text(cx, y, pen.fit(line, FS, width), FS, align="c")
            y += 10
        base = y + 3
        if text:
            pen.text(cx, base + 6, pen.fit(text, FS, width), FS, align="c")
        if image:
            try:
                img = ImageReader(BytesIO(image))
                iw, ih = img.getSize()
                scale = min(min(width, 130) / iw, max(top - base - 6, 10) / ih, 44 / ih)
                w, h = iw * scale, ih * scale
                self.c.drawImage(img, cx - w / 2, base, width=w, height=h, mask="auto")
            except Exception:  # a bad image must never stop a document printing
                pass
