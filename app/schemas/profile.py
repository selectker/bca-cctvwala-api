import re

from pydantic import BaseModel, Field, field_validator

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UpdateProfileRequest(BaseModel):
    """Only the fields sent change. `name` and `phone` can't be cleared; `email` can (send null or "")."""

    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=15)
    email: str | None = Field(default=None, max_length=150)

    @field_validator("name", "phone")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is not None and len(v) < 5:
            raise ValueError("must be at least 5 characters")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not _EMAIL.match(v):
            raise ValueError("is not a valid email address")
        return v


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
