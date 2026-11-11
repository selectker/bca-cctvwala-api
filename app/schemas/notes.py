from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.schemas.billing import PaymentMode

Serial = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class NoteLineInput(BaseModel):
    """One line being returned. `item_id` is the invoice line (credit note) or purchase line (debit note) it comes from."""
    item_id: str
    quantity: Decimal = Field(gt=0, le=1_000_000, max_digits=12, decimal_places=2)
    barcodes: list[Serial] | None = Field(default=None, max_length=1000)  # serial numbers coming back (serial-tracked lines)


class CreateCreditNoteRequest(BaseModel):
    invoice_id: str
    reason: str = Field(min_length=1, max_length=500)
    note_date: date | None = None
    # True: the goods come back into stock. False: they are damaged / scrapped (no stock added, serial units SCRAPPED).
    restock: bool = True
    # How any money that has to go back to the customer is paid out (only used when the credit is more than they still owe).
    refund_mode: PaymentMode = "CASH"
    notes: str | None = Field(default=None, max_length=2000)
    items: list[NoteLineInput] = Field(min_length=1, max_length=200)


class CreateDebitNoteRequest(BaseModel):
    purchase_id: str
    reason: str = Field(min_length=1, max_length=500)
    note_date: date | None = None
    refund_mode: PaymentMode = "CASH"
    notes: str | None = Field(default=None, max_length=2000)
    items: list[NoteLineInput] = Field(min_length=1, max_length=200)


class CancelNoteRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
