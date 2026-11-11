from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.products import HsnCode
from app.schemas.types import Address, BarcodeList, DiscountPercent, NonNegativeMoney, GstRate, Id, IdList, LongText, MAX_LINES, MAX_LINE_VALUE, OptCode, OptId, Phone, PositiveMoney, Quantity, UnitPrice
from app.utils.dates import today_local

PaymentMode = Literal["CASH", "UPI", "BANK", "CHEQUE", "CARD"]


# ---------- Purchases ----------
def _check_line_value(quantity: Decimal, unit_price: Decimal) -> None:
    if quantity * unit_price > MAX_LINE_VALUE:
        raise ValueError("quantity x unit_price is too large for a single line")


class PurchaseItemLine(BaseModel):
    product_id: Id
    quantity: Quantity
    unit_price: UnitPrice
    gst_rate: GstRate
    # For serialized products: one barcode per unit received (len must equal quantity).
    # A part_warranties row is created for each one automatically, linked to this purchase.
    barcodes: BarcodeList | None = None
    supplier_warranty_months: int | None = Field(default=None, ge=0, le=240)  # applied to every barcode in this line

    @model_validator(mode="after")
    def _line_value(self):
        _check_line_value(self.quantity, self.unit_price)
        return self


class CreatePurchaseRequest(BaseModel):
    supplier_id: Id
    purchase_date: date | None = None  # backdate a purchase; defaults to today
    supplier_invoice_no: OptCode = None
    notes: LongText = Field(default=None, max_length=2000)
    items: list[PurchaseItemLine] = Field(max_length=MAX_LINES)


# ---------- Quotations & Invoices share the same line-item shape ----------
class LineItemInput(BaseModel):
    product_id: OptId = None
    description: LongText = None
    quantity: Quantity
    unit_price: UnitPrice
    gst_rate: GstRate
    # Trade discount on this line, in percent of quantity x unit_price (0-100). GST is charged on the
    # discounted value. Left out = no discount.
    discount_percent: DiscountPercent = Decimal("0")
    # For serialized products being sold: the exact barcode(s) leaving stock (len must equal quantity).
    # Each barcode's part_warranties row is matched, checked IN_STOCK, and flipped to SOLD against
    # this invoice automatically — this is what keeps purchase -> warranty -> sale linked.
    barcodes: BarcodeList | None = None
    customer_warranty_months: int | None = Field(default=None, ge=0, le=240)  # applied to every barcode in this line
    # HSN / SAC printed for this line. Left out: a product line uses the product's HSN; a free-text service line
    # has none. Only used on invoices (quotation lines show the product's HSN).
    hsn_code: HsnCode = None
    # Custom (no product) lines only: what one unit cost you, before GST. Only used by the profit-loss report, so profit
    # on the line is sale - cost. Left out = unknown, the line counts as cost 0. Ignored when a product is picked.
    cost_price: Decimal | None = Field(default=None, ge=0, le=Decimal("99999999.99"), max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def _line_value(self):
        _check_line_value(self.quantity, self.unit_price)
        return self


class _InvoiceDiscountMixin(BaseModel):
    # Discount on the whole document (goods / custom lines; billed service tickets are not discounted), on top of
    # any line discounts. Either a percent or a flat rupee amount, not both. It is spread over the lines before GST.
    invoice_discount_percent: DiscountPercent = Decimal("0")
    invoice_discount_amount: NonNegativeMoney = Decimal("0")

    @model_validator(mode="after")
    def _one_invoice_discount(self):
        if self.invoice_discount_percent > 0 and self.invoice_discount_amount > 0:
            raise ValueError("Give the discount as a percent or as an amount, not both")
        return self


class _DueDateMixin(BaseModel):
    # When the customer has to pay. Give the number of days from today (payment_terms_days) or the date itself,
    # not both. Left out = the customer's default terms; no default either = due on the invoice date.
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    due_date: date | None = None
    # A new invoice that takes the customer over their credit limit is refused (409) unless this is true.
    allow_over_credit_limit: bool = False

    @model_validator(mode="after")
    def _terms_or_date(self):
        if self.payment_terms_days is not None and self.due_date is not None:
            raise ValueError("Give payment terms (days) or a due date, not both")
        if self.due_date is not None and self.due_date < today_local():
            raise ValueError("The due date can't be in the past")
        return self


class CreateQuotationRequest(_InvoiceDiscountMixin):
    customer_id: Id
    valid_till: date | None = None
    notes: LongText = Field(default=None, max_length=2000)
    items: list[LineItemInput] = Field(max_length=MAX_LINES)


class UpdateQuotationStatusRequest(BaseModel):
    status: Literal["PENDING", "ACCEPTED", "REJECTED"]  # CONVERTED is only set by converting


class CreateInvoiceRequest(_InvoiceDiscountMixin, _DueDateMixin):
    customer_id: Id
    invoice_type: str = "SALE"
    quotation_id: OptId = None
    # Only for the built-in walk-in customer: who actually bought, printed in the PDF's Bill To box (both optional).
    # Sending either for any other customer is refused (422).
    buyer_name: str | None = Field(default=None, max_length=150)
    buyer_phone: Phone = None
    # Printed on the invoice PDF. Left out (null) = the customer's saved billing / shipping address at this moment;
    # an empty shipping address means "same as billing". Each invoice keeps its own copy.
    billing_address: Address = Field(default=None, max_length=500)
    shipping_address: Address = Field(default=None, max_length=500)
    notes: LongText = Field(default=None, max_length=2000)
    items: list[LineItemInput] = Field(max_length=MAX_LINES)
    # Selling more than is in stock is refused (409). Set this to true to sell anyway: the sale goes
    # through, stock goes negative, and the response carries a warning for each affected product.
    allow_negative_stock: bool = False
    # OWNER only, SERVICE invoices only: the attended/closed PAID service tickets this invoice bills.
    # Each ticket can be billed once; the price lines themselves are still sent in `items`.
    service_ticket_ids: IdList | None = None
    # With service_ticket_ids: add one price line per ticket from its charge_amount / gst_rate
    # ("Service <ticket no>: <title>", quantity 1) in front of `items`, so the owner doesn't retype the price.
    # `items` may then be empty; a ticket with no charge amount is refused (400).
    include_ticket_charges: bool = False


class UpdateInvoiceRequest(_InvoiceDiscountMixin):
    """PUT /invoices/{id} (OWNER only): correct a live invoice. The customer, the invoice number and the invoice type
    stay as they are. `items` replaces ALL the lines (send every line that should remain, even if unchanged); stock,
    serial-number warranties, GST and totals are redone from them. Every other field is optional: left out = unchanged
    (for the addresses, notes and due date, null clears the value; the discount is only changed when one of its two
    fields is sent). Payments already received stay on the invoice."""
    items: list[LineItemInput] = Field(min_length=1, max_length=MAX_LINES)
    invoice_date: date | None = None  # can't be in the future; leave out to keep it
    # Walk-in invoices only (refused 422 on any other): left out = unchanged, null / "" clears it.
    buyer_name: str | None = Field(default=None, max_length=150)
    buyer_phone: Phone = None
    billing_address: Address = Field(default=None, max_length=500)
    shipping_address: Address = Field(default=None, max_length=500)
    notes: LongText = Field(default=None, max_length=2000)
    # Due date: terms (days from the invoice date) or the date itself, not both; both null removes the due date.
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    due_date: date | None = None
    # Same two safety switches as when making an invoice.
    allow_negative_stock: bool = False
    allow_over_credit_limit: bool = False

    @model_validator(mode="after")
    def _terms_or_date(self):
        if self.payment_terms_days is not None and self.due_date is not None:
            raise ValueError("Give payment terms (days) or a due date, not both")
        return self


class ConvertItemOverride(BaseModel):
    quotation_item_id: Id
    barcodes: BarcodeList | None = None
    customer_warranty_months: int | None = Field(default=None, ge=0, le=240)


class ConvertQuotationRequest(_DueDateMixin):
    """Optional body for POST /invoices/from-quotation/{id}: supply the physical serials (and
    warranty length) for serialized lines so they're linked to warranties on the new invoice."""
    items: list[ConvertItemOverride] = []
    notes: LongText = Field(default=None, max_length=2000)
    allow_negative_stock: bool = False  # same meaning as on CreateInvoiceRequest
    billing_address: Address = Field(default=None, max_length=500)  # same meaning as on CreateInvoiceRequest
    shipping_address: Address = Field(default=None, max_length=500)


class PaymentRequest(BaseModel):
    amount: PositiveMoney
    payment_mode: PaymentMode = "CASH"
    payment_date: date | None = None  # defaults to today; can't be in the future
    reference_no: str | None = Field(default=None, max_length=50)  # cheque no / UTR / txn id
    notes: LongText = Field(default=None, max_length=2000)

    @field_validator("payment_date")
    @classmethod
    def _not_in_future(cls, v: date | None) -> date | None:
        if v is not None and v > today_local():
            raise ValueError("can't be in the future")
        return v


class UpdateDueDateRequest(BaseModel):
    """PUT /invoices/{id}/due-date: change when an invoice is due. Send payment_terms_days (counted from the invoice
    date) or due_date, not both; both null removes the due date (the invoice is then due on its own date)."""
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    due_date: date | None = None

    @model_validator(mode="after")
    def _terms_or_date(self):
        if self.payment_terms_days is not None and self.due_date is not None:
            raise ValueError("Give payment terms (days) or a due date, not both")
        if not self.model_fields_set:
            raise ValueError("Send payment_terms_days or due_date (both null removes the due date)")
        return self


class UpdatePaymentRequest(BaseModel):
    """PUT /invoices/{id}/payments/{payment_id} (and the purchase equivalent): fix how / when a payment was made. The
    amount can't be edited -- reverse the payment and record it again. Fields left out keep their value;
    reference_no and notes can be cleared by sending null."""
    payment_mode: PaymentMode | None = None
    payment_date: date | None = None
    reference_no: str | None = Field(default=None, max_length=50)
    notes: LongText = Field(default=None, max_length=2000)

    @field_validator("payment_date")
    @classmethod
    def _not_in_future(cls, v: date | None) -> date | None:
        if v is not None and v > today_local():
            raise ValueError("can't be in the future")
        return v

    @model_validator(mode="after")
    def _not_null(self):
        for name in ("payment_mode", "payment_date"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can't be null")
        if not self.model_fields_set:
            raise ValueError("Nothing to change")
        return self


class ReversePaymentRequest(BaseModel):
    """POST /invoices/{id}/payments/{payment_id}/reverse (and the purchase equivalent)."""
    reason: str = Field(min_length=1, max_length=500)


class CancelDocumentRequest(BaseModel):
    """Body for POST /invoices/{id}/cancel and POST /purchases/{id}/cancel (OWNER only)."""
    reason: str = Field(min_length=1, max_length=500)
    # If money was already recorded against the document, the owner must say it was handed back
    # (a negative "refund" payment is booked so the payment history nets to zero).
    refund_payments: bool = False


class UpdatePurchaseRequest(BaseModel):
    """PUT /purchases/{id}: fix the header only. Fields left out keep their value; supplier_invoice_no
    and notes can be cleared by sending null. Lines can't be edited -- cancel and re-enter instead,
    because they drive stock and unit warranties."""
    supplier_id: OptId = None
    purchase_date: date | None = None
    supplier_invoice_no: OptCode = Field(default=None, max_length=50)
    notes: LongText = Field(default=None, max_length=2000)
