"""Field-level checks shared by the request schemas (GSTIN, GST state code, phone, e-mail, names)."""
import re

# GST state / UT codes: 01-38 plus 97 (other territory) and 99 (centre jurisdiction).
_STATE_CODE = re.compile(r"(0[1-9]|[12]\d|3[0-8]|97|99)")
# 2-digit state code + 10-char PAN + entity number + 'Z' + check character.
_GSTIN = re.compile(r"(0[1-9]|[12]\d|3[0-8]|97|99)[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_JUNK = re.compile(r"[\s\-().]")

PHONE_MAX_LEN = 15  # VARCHAR(15) in the database


def clean_optional(value: str | None) -> str | None:
    """Strip; blank -> None."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def validate_gstin(value: str | None) -> str | None:
    value = clean_optional(value)
    if value is None:
        return None
    value = value.upper()
    if not _GSTIN.fullmatch(value):
        raise ValueError("is not a valid 15-character GSTIN (e.g. 29ABCDE1234F1Z5)")
    return value


def validate_state_code(value: str | None) -> str | None:
    value = clean_optional(value)
    if value is None:
        return None
    if not _STATE_CODE.fullmatch(value):
        raise ValueError("must be a valid 2-digit GST state code, e.g. 29")
    return value


def validate_phone(value: str | None) -> str | None:
    """Spaces, dashes, dots and brackets are dropped; what's left must be an optional leading '+'
    and 7-15 digits (and fit the 15-character column)."""
    value = clean_optional(value)
    if value is None:
        return None
    value = _PHONE_JUNK.sub("", value)
    digits = value[1:] if value.startswith("+") else value
    if not digits.isascii() or not digits.isdigit() or not 7 <= len(digits) <= PHONE_MAX_LEN or len(value) > PHONE_MAX_LEN:
        raise ValueError("must be a phone number of 7-15 digits (an optional leading + is allowed)")
    return value


def validate_email(value: str | None) -> str | None:
    value = clean_optional(value)
    if value is None:
        return None
    if not _EMAIL.fullmatch(value):
        raise ValueError("is not a valid email address")
    return value
