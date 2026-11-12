from datetime import date

from pydantic import BaseModel

from app.schemas.types import Code, Id, LongText, OptCode, OptId


class CreateWarrantyRequest(BaseModel):
    product_id: Id
    barcode: OptCode = None  # the actual physical barcode/serial printed on the part
    purchase_id: OptId = None
    purchase_item_id: OptId = None
    purchase_date: date | None = None
    supplier_warranty_months: int | None = None
    supplier_warranty_valid_until: date | None = None  # overrides supplier_warranty_months if both given
    notes: LongText = None


class UpdateWarrantyRequest(BaseModel):
    barcode: OptCode = None
    product_id: OptId = None
    supplier_warranty_months: int | None = None
    supplier_warranty_valid_until: date | None = None
    customer_warranty_months: int | None = None
    customer_warranty_valid_until: date | None = None
    # Manual status changes are limited to IN_STOCK / RETURNED / SCRAPPED (and leaving them again);
    # SOLD, CLAIMED and REPLACED only happen through sell / claim / replace so the links stay intact.
    status: OptCode = None
    notes: LongText = None


class SellWarrantyRequest(BaseModel):
    invoice_id: OptId = None
    invoice_item_id: OptId = None
    sale_date: date | None = None
    customer_warranty_months: int | None = None
    customer_warranty_valid_until: date | None = None  # overrides customer_warranty_months if both given


class ClaimWarrantyRequest(BaseModel):
    notes: LongText = None
    # If the customer is being handed a replacement unit right away, pass the barcode of an
    # IN_STOCK unit of the same product here — it's swapped onto the customer's invoice with
    # the same remaining warranty period, and this unit is marked REPLACED instead of CLAIMED.
    replacement_barcode: OptCode = None


class ReplaceWarrantyRequest(BaseModel):
    # For a unit that was already claimed earlier (e.g. sent back to the supplier and you're
    # only now getting stock to replace it) — same swap as above, done as a separate step.
    replacement_barcode: Code
    notes: LongText = None
