"""Shared maths for credit notes (sales returns) and debit notes (purchase returns)."""
from decimal import Decimal

from fastapi import HTTPException, status

from app.utils.gst import round2

ZERO = Decimal("0.00")


def share(total: Decimal, line_qty: Decimal, qty: Decimal, remaining: Decimal, already: Decimal) -> Decimal:
    """The part of a line's `total` that `qty` of its `line_qty` units carries. Returning everything that is left takes
    exactly what is left (total - already returned), so many partial returns never drift by a paisa from the original."""
    if qty >= remaining:
        return round2(total - already)
    return round2(total * qty / line_qty)


def check_quantity(qty: Decimal, remaining: Decimal, what: str) -> None:
    if qty > remaining:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{what}: only {remaining.normalize():f} left to return, but {qty.normalize():f} was asked",
        )


def settle(total: Decimal, doc_total, doc_paid) -> tuple[Decimal, Decimal]:
    """(applied, refund): how a note of `total` is used up. It first cancels what is still owed on the document
    (applied); anything beyond that is money that has to go back (refund)."""
    owed = max(round2(doc_total) - round2(doc_paid), ZERO)
    applied = min(total, owed)
    return applied, round2(total - applied)


def status_for(total, paid) -> str:
    if paid >= total:
        return "PAID"
    return "PARTIAL" if paid > 0 else "UNPAID"
