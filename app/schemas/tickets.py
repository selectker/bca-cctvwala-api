from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.types import Address, GstRate, Id, LongText, NonNegativeMoney, OptCode, OptId

ServiceType = Literal["IN_WARRANTY", "PAID", "AMC"]
TicketStatus = Literal["OPEN", "RESCHEDULED", "ATTENDED", "CLOSED", "CANCELLED"]


class CreateTicketRequest(BaseModel):
    customer_id: Id
    service_type: ServiceType
    title: str = Field(min_length=1, max_length=200)
    description: LongText = None
    site_address: Address = None  # defaults to the customer's address
    # Optional: the tracked unit (part_warranties.barcode) the request is about. For an IN_WARRANTY
    # ticket the unit must have been sold to this customer and its customer warranty must still be valid.
    warranty_barcode: OptCode = None
    # Required for service_type AMC (and not allowed for any other type): the AMC contract that covers this request. It
    # must belong to this customer, be ACTIVE and be in its term today.
    amc_id: OptId = None
    charge_amount: NonNegativeMoney | None = None  # estimate, ex-GST; PAID only
    gst_rate: GstRate = Decimal("18.00")
    assigned_to: OptId = None  # STAFF / TECHNICIAN user; empty = any of them may pick it up
    scheduled_date: date | None = None


class CreateAmcTicketRequest(BaseModel):
    """POST /amc/{id}/tickets: raise a ticket under that contract. The customer comes from the contract, the type is AMC and
    there is no charge; the site defaults to the contract's site."""
    title: str = Field(min_length=1, max_length=200)
    description: LongText = None
    site_address: Address = None
    warranty_barcode: OptCode = None
    assigned_to: OptId = None
    scheduled_date: date | None = None


class UpdateTicketRequest(BaseModel):
    """OWNER only, while the ticket is OPEN / RESCHEDULED / ATTENDED and not yet billed.
    Fields left out keep their value; nullable ones can be cleared by sending null."""
    service_type: ServiceType | None = None
    # With service_type AMC: the covering contract (kept as it is when left out). Sending a contract for another type is a 400.
    amc_id: OptId = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: LongText = None
    site_address: Address = None
    charge_amount: NonNegativeMoney | None = None
    gst_rate: GstRate | None = None
    assigned_to: OptId = None
    scheduled_date: date | None = None


class RescheduleTicketRequest(BaseModel):
    new_date: date
    reason: str = Field(min_length=1, max_length=500)


class CloseTicketRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class CancelTicketRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ReopenTicketRequest(BaseModel):
    """OWNER only: send an attended (not yet closed, not billed) ticket back to the technician."""
    reason: str = Field(min_length=1, max_length=500)
    scheduled_date: date | None = None  # optional new visit date
