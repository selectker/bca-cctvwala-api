from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.types import Address, GstRate, Id, NonNegativeMoney, OptId


class CreateAmcRequest(BaseModel):
    customer_id: Id
    site_address: Address = None
    start_date: date
    end_date: date
    visit_frequency: Literal["MONTHLY", "QUARTERLY", "HALF_YEARLY", "YEARLY"] = "QUARTERLY"
    amount: NonNegativeMoney
    gst_rate: GstRate = Decimal("18.00")
    coverage_notes: str | None = Field(default=None, max_length=2000)


class UpdateAmcRequest(BaseModel):
    """Only the fields sent change. The customer can't be changed (make a new contract instead)."""

    site_address: Address = None
    start_date: date | None = None
    end_date: date | None = None
    visit_frequency: Literal["MONTHLY", "QUARTERLY", "HALF_YEARLY", "YEARLY"] | None = None
    amount: NonNegativeMoney | None = None
    gst_rate: GstRate | None = None
    coverage_notes: str | None = Field(default=None, max_length=2000)


class RenewAmcRequest(BaseModel):
    """Everything is optional: left out, it is carried over from the contract being renewed. The new term starts the
    day after the old one ends and lasts as long as the old one did."""

    start_date: date | None = None
    end_date: date | None = None
    visit_frequency: Literal["MONTHLY", "QUARTERLY", "HALF_YEARLY", "YEARLY"] | None = None
    amount: NonNegativeMoney | None = None
    gst_rate: GstRate | None = None
    site_address: Address = None
    coverage_notes: str | None = Field(default=None, max_length=2000)


class UpdateAmcStatusRequest(BaseModel):
    status: Literal["ACTIVE", "EXPIRED", "CANCELLED"]


class UpdateAmcVisitRequest(BaseModel):
    # Fields left out of the request keep their current value (technician_id / remarks).
    status: Literal["SCHEDULED", "COMPLETED", "MISSED"]
    completed_date: date | None = None
    technician_id: OptId = None
    remarks: str | None = Field(default=None, max_length=2000)
