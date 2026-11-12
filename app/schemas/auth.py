from typing import Literal

from pydantic import BaseModel, Field, model_validator

Role = Literal["OWNER", "STAFF", "TECHNICIAN"]
StaffRole = Literal["STAFF", "TECHNICIAN"]
PIN_PATTERN = r"^\d{6}$"
# bcrypt only looks at the first 72 bytes anyway; a cap keeps oversized bodies away from the hashing step.
MAX_PASSWORD_LENGTH = 128
from app.schemas.types import Id


class BootstrapRequest(BaseModel):
    """Shared shape for both bootstrap steps: the platform admin (first account) and the
    owner (second account, after which self-signup of either is permanently blocked)."""

    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=5, max_length=15)
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_LENGTH)


class CreateStaffRequest(BaseModel):
    """Owner-only. No password is collected here -- the server generates a one-time PIN."""

    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=5, max_length=15)
    role: StaffRole


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: StaffRole | None = None
    is_active: bool | None = None


class ChangePinRequest(BaseModel):
    current_pin: str = Field(pattern=PIN_PATTERN)
    new_pin: str = Field(pattern=PIN_PATTERN)


class LoginRequest(BaseModel):
    phone: str = Field(max_length=40)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class LoginPinRequest(BaseModel):
    """Staff / technician sign-in: their mobile number (what they remember) or, as before, their account ID."""

    phone: str | None = Field(default=None, max_length=40)
    account_id: Id | None = Field(default=None, max_length=64)
    pin: str = Field(pattern=PIN_PATTERN)

    @model_validator(mode="after")
    def _one_identifier(self):
        if not (self.phone and self.phone.strip()) and not (self.account_id and self.account_id.strip()):
            raise ValueError("Enter your mobile number (or account ID)")
        return self
