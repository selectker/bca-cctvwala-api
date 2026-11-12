from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.config import settings

STORE_STATE_CODE = settings.store_state_code  # fallback when the store profile has no state_code

_PAISA = Decimal("0.01")
_RUPEE = Decimal("1")
# The money columns are NUMERIC(12,2): anything above this would fail at the database with a 500.
MAX_DOCUMENT_TOTAL = Decimal("9999999999.99")


class AmountTooLarge(ValueError):
    """A document total that doesn't fit the money columns. main.py turns it into a 400."""


async def get_store_state_code(conn) -> str:
    """State code used to decide CGST+SGST vs IGST. Read from the store profile (editable in the
    app) and falling back to the STORE_STATE_CODE env setting."""
    code = await conn.fetchval("SELECT state_code FROM store_profile WHERE id = 1")
    return code or STORE_STATE_CODE


def to_decimal(value) -> Decimal:
    """Decimal from a Decimal / int / float / str. Floats go through str() so 0.1 stays 0.1."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round2(value) -> Decimal:
    """Round to paise, halves away from zero (the usual invoicing rule), never via binary floats."""
    return to_decimal(value).quantize(_PAISA, rounding=ROUND_HALF_UP)


@dataclass
class LineGst:
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    line_total: Decimal
    is_interstate: bool
    discount_amount: Decimal = Decimal("0.00")  # taken off quantity x unit_price before GST


def compute_line_gst(quantity, unit_price, gst_rate, buyer_state_code: str | None,
                     store_state_code: str | None = None, discount_percent=0,
                     extra_discount=0) -> LineGst:
    """
    Same rule as the Node version: same state as the store -> CGST+SGST (half rate each),
    different state -> IGST (full rate). All arithmetic is Decimal.

    `discount_percent` (0-100) is a trade discount on the line: it is taken off quantity x unit_price
    first, so GST is charged on the discounted (taxable) value. `extra_discount` is a rupee amount on top of
    that (this line's share of an invoice-level discount); it is folded into `discount_amount`.
    """
    quantity, unit_price, gst_rate = to_decimal(quantity), to_decimal(unit_price), to_decimal(gst_rate)
    gross = round2(quantity * unit_price)
    discount_amount = round2(gross * to_decimal(discount_percent or 0) / 100) + round2(extra_discount or 0)
    if discount_amount > gross:
        raise ValueError("A line's discount can't be more than its value")
    taxable_value = round2(gross - discount_amount)
    is_interstate = bool(buyer_state_code and buyer_state_code != (store_state_code or STORE_STATE_CODE))

    cgst = sgst = igst = Decimal("0.00")
    if is_interstate:
        igst = round2(taxable_value * gst_rate / 100)
    else:
        cgst = round2(taxable_value * gst_rate / 200)
        sgst = round2(taxable_value * gst_rate / 200)

    line_total = round2(taxable_value + cgst + sgst + igst)
    return LineGst(taxable_value, cgst, sgst, igst, line_total, is_interstate, discount_amount)


@dataclass
class InvoiceTotals:
    subtotal: Decimal
    total_cgst: Decimal
    total_sgst: Decimal
    total_igst: Decimal
    round_off: Decimal
    total_amount: Decimal
    total_discount: Decimal = Decimal("0.00")  # already deducted from `subtotal` (which is the taxable value)


def compute_invoice_totals(lines: list[LineGst]) -> InvoiceTotals:
    subtotal = round2(sum((l.taxable_value for l in lines), Decimal("0")))
    total_cgst = round2(sum((l.cgst for l in lines), Decimal("0")))
    total_sgst = round2(sum((l.sgst for l in lines), Decimal("0")))
    total_igst = round2(sum((l.igst for l in lines), Decimal("0")))
    raw_total = subtotal + total_cgst + total_sgst + total_igst
    rounded_total = raw_total.quantize(_RUPEE, rounding=ROUND_HALF_UP)
    round_off = round2(rounded_total - raw_total)
    if rounded_total > MAX_DOCUMENT_TOTAL:
        raise AmountTooLarge("The document total is too large")

    total_discount = round2(sum((l.discount_amount for l in lines), Decimal("0")))
    return InvoiceTotals(subtotal, total_cgst, total_sgst, total_igst, round_off, round2(rounded_total), total_discount)


def compute_lines_with_invoice_discount(specs, buyer_state_code, store_state_code=None,
                                        invoice_discount_percent=0, invoice_discount_amount=0):
    """
    `specs` is a list of (quantity, unit_price, gst_rate, line_discount_percent). Returns (lines, invoice_discount).

    An invoice-level discount (a percent of the goods value, or a flat rupee amount; flat wins if both are given)
    is spread over the lines in proportion to their value after line discounts, BEFORE GST, so GST is charged on
    what the customer really pays and the CGST/SGST/IGST and GSTR-1 figures stay correct. Each line's share is
    added to its `discount_amount`; the last line absorbs the rounding remainder so the shares add up exactly.
    Raises ValueError if the discount is more than the goods value.
    """
    args = (buyer_state_code, store_state_code)
    base = [compute_line_gst(q, p, g, *args, d) for q, p, g, d in specs]
    total = sum((l.taxable_value for l in base), Decimal("0"))
    flat = round2(invoice_discount_amount or 0)
    discount = flat if flat > 0 else round2(total * to_decimal(invoice_discount_percent or 0) / 100)
    if discount <= 0:
        return base, Decimal("0.00")
    if discount > total:
        raise ValueError("The invoice discount can't be more than the invoice value")
    last = max(i for i, l in enumerate(base) if l.taxable_value > 0)
    shares, used = [Decimal("0.00")] * len(base), Decimal("0.00")
    for i, l in enumerate(base):
        if l.taxable_value <= 0 or i == last:
            continue
        shares[i] = min(round2(discount * l.taxable_value / total), l.taxable_value)
        used += shares[i]
    shares[last] = discount - used
    lines = [compute_line_gst(q, p, g, *args, d, shares[i]) for i, (q, p, g, d) in enumerate(specs)]
    return lines, discount
