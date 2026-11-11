from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.schemas.billing import PaymentMode
from app.schemas.types import LongText, PositiveMoney
from app.utils.dates import today_local

ExpenseCategory = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class ExpenseRequest(BaseModel):
    """One business expense. `amount` is the total paid (including any GST)."""

    expense_date: date | None = None  # defaults to today; can't be in the future
    category: ExpenseCategory
    amount: PositiveMoney
    payment_mode: PaymentMode = "CASH"
    paid_to: str | None = Field(default=None, max_length=120)
    reference_no: str | None = Field(default=None, max_length=50)  # cheque no / UTR / bill no
    notes: LongText = Field(default=None, max_length=2000)

    @field_validator("expense_date")
    @classmethod
    def _not_in_future(cls, v: date | None) -> date | None:
        if v is not None and v > today_local():
            raise ValueError("can't be in the future")
        return v
