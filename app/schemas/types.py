"""Reusable constrained field types for the request schemas.

Money, quantities and rates are Decimal (never float) and carry at most the number of decimal places
their NUMERIC column stores, so nothing is silently rounded by the database.
"""
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

from app.utils.validation import validate_email, validate_gstin, validate_phone, validate_state_code

# Highest GST slab in force is 40%. Anything above is a typo (or an attempt to inflate an invoice).
MAX_GST_RATE = Decimal("40")
MAX_QUANTITY = Decimal("100000")
MAX_UNIT_PRICE = Decimal("10000000")  # Rs. 1 crore per unit
MAX_LINE_VALUE = Decimal("100000000")  # Rs. 10 crore per line, before GST
MAX_MONEY = Decimal("9999999999.99")  # largest value of a NUMERIC(12,2) column
MAX_LINES = 500

Quantity = Annotated[Decimal, Field(gt=0, le=MAX_QUANTITY, max_digits=12, decimal_places=2)]
UnitPrice = Annotated[Decimal, Field(ge=0, le=MAX_UNIT_PRICE, max_digits=12, decimal_places=2)]
DiscountPercent = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
GstRate = Annotated[Decimal, Field(ge=0, le=MAX_GST_RATE, max_digits=5, decimal_places=2)]
PositiveMoney = Annotated[Decimal, Field(gt=0, le=MAX_MONEY, max_digits=12, decimal_places=2)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, le=MAX_MONEY, max_digits=12, decimal_places=2)]
# Opening balances may legitimately be negative (an advance received / paid).
SignedMoney = Annotated[Decimal, Field(ge=-MAX_MONEY, le=MAX_MONEY, max_digits=12, decimal_places=2)]

PartyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Gstin = Annotated[str | None, AfterValidator(validate_gstin)]
StateCode = Annotated[str | None, Field(max_length=10), AfterValidator(validate_state_code)]
Phone = Annotated[str | None, Field(max_length=30), AfterValidator(validate_phone)]
Email = Annotated[str | None, Field(max_length=150), AfterValidator(validate_email)]

# Free-text and identifier caps. Without them the only limit on a string is the request body limit.
Id = Annotated[str, Field(max_length=64)]
OptId = Annotated[str | None, Field(max_length=64)]
IdList = Annotated[list[Id], Field(max_length=MAX_LINES)]
Code = Annotated[str, Field(max_length=100)]  # barcode / serial / supplier's invoice number
OptCode = Annotated[str | None, Field(max_length=100)]
BarcodeList = Annotated[list[Code], Field(max_length=5000)]
Address = Annotated[str | None, Field(max_length=500)]
LongText = Annotated[str | None, Field(max_length=2000)]

# GET ?search= filters: long enough for any invoice number, name or barcode.
MAX_SEARCH_LENGTH = 100
