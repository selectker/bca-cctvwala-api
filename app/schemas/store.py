import re

from pydantic import BaseModel, Field, field_validator

from app.schemas.types import Address, Email, Gstin, Phone, StateCode


class SignatureBase64Request(BaseModel):
    """The signature image as base64: raw, or a `data:image/png;base64,...` data URL."""
    signature_base64: str = Field(min_length=1)


class UpdateStoreProfileRequest(BaseModel):
    store_name: str = Field(min_length=1, max_length=150)
    gstin: Gstin = None
    address: Address = None
    state: str | None = Field(default=None, max_length=50)
    state_code: StateCode = None  # decides CGST+SGST vs IGST
    phone: Phone = None
    email: Email = None
    bank_name: str | None = Field(default=None, max_length=150)
    bank_account_no: str | None = Field(default=None, max_length=30)
    bank_ifsc: str | None = Field(default=None, max_length=15)
    # The shop's UPI ID, like name@bank. Left out = unchanged (an older app doesn't send it); "" or null clears it.
    upi_id: str | None = Field(default=None, max_length=100)
    invoice_prefix: str = Field(default="INV", max_length=10)
    quotation_prefix: str = Field(default="QUO", max_length=10)

    @field_validator("upi_id")
    @classmethod
    def _upi_id(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if v and not re.fullmatch(r"[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9.-]{1,48}", v):
            raise ValueError("not a UPI ID (expected something like shop@okbank)")
        return v or None

    @field_validator("invoice_prefix", "quotation_prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{1,10}", v):
            raise ValueError("use 1-10 letters, digits or dashes")
        return v
