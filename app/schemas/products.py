from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.schemas.types import GstRate, MAX_QUANTITY, NonNegativeMoney, OptId
from app.utils.validation import clean_optional

ProductName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
# HSN codes are 2-8 digits (the database column is VARCHAR(10)); blank = none.
# The constraints come BEFORE clean_optional: it turns "" into None, and a length / pattern check that ran after it
# would be applied to None, which pydantic reports as a TypeError (a 500) instead of a validation error.
HsnCode = Annotated[str | None, Field(pattern=r"^(\d{2,8})?$", max_length=10), AfterValidator(clean_optional)]
_Sku = Annotated[str | None, Field(max_length=50)]
_Brand = Annotated[str | None, Field(max_length=100)]
_Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class CreateProductRequest(BaseModel):
    sku: _Sku = None
    name: ProductName
    category_id: OptId = None
    brand: _Brand = None
    hsn_code: HsnCode = None
    unit: _Unit = "PCS"
    gst_rate: GstRate = Decimal("18.00")
    purchase_price: NonNegativeMoney = Decimal("0")
    selling_price: NonNegativeMoney = Decimal("0")
    reorder_level: int = Field(default=0, ge=0, le=1_000_000)
    is_serialized: bool = False


class UpdateProductRequest(BaseModel):
    sku: _Sku = None
    name: ProductName
    category_id: OptId = None
    brand: _Brand = None
    hsn_code: HsnCode = None
    unit: _Unit
    gst_rate: GstRate
    purchase_price: NonNegativeMoney | None = None  # unchanged when omitted
    selling_price: NonNegativeMoney
    reorder_level: int = Field(ge=0, le=1_000_000)
    is_serialized: bool = False


class StockAdjustRequest(BaseModel):
    """Signed: positive adds stock, negative removes it (it can't take the stock below zero)."""
    quantity: Decimal = Field(max_digits=12, decimal_places=2, ge=-MAX_QUANTITY, le=MAX_QUANTITY)
    note: str | None = Field(default=None, max_length=500)


class CreateCategoryRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
