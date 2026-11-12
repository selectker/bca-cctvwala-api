from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.types import Address, Email, Gstin, NonNegativeMoney, PartyName, Phone, SignedMoney, StateCode

CustomerType = Literal["B2B", "B2C"]


class CreateCustomerRequest(BaseModel):
    name: PartyName
    gstin: Gstin = None
    phone: Phone = None
    email: Email = None
    address: Address = Field(default=None, max_length=500)
    # The billing address is `address`. Empty shipping address = same as billing.
    shipping_address: Address = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=50)
    state_code: StateCode = None
    customer_type: CustomerType = "B2C"
    opening_balance: SignedMoney = 0
    # Most this customer may owe at once (null = no limit); a new invoice that would take them over is refused
    # unless the owner confirms. Default payment terms (days) give new invoices their due date.
    credit_limit: NonNegativeMoney | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)


class UpdateCustomerRequest(BaseModel):
    name: PartyName
    gstin: Gstin = None
    phone: Phone = None
    email: Email = None
    address: Address = Field(default=None, max_length=500)
    # The billing address is `address`. Empty shipping address = same as billing.
    shipping_address: Address = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=50)
    state_code: StateCode = None
    customer_type: CustomerType | None = None
    # Left out = unchanged (an older app doesn't send them); null clears the limit / terms.
    credit_limit: NonNegativeMoney | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)


class CreateSupplierRequest(BaseModel):
    name: PartyName
    gstin: Gstin = None
    phone: Phone = None
    email: Email = None
    address: Address = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=50)
    state_code: StateCode = None
    opening_balance: SignedMoney = 0


class UpdateSupplierRequest(BaseModel):
    name: PartyName
    gstin: Gstin = None
    phone: Phone = None
    email: Email = None
    address: Address = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=50)
    state_code: StateCode = None
