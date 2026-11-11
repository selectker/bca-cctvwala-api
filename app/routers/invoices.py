import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.billing import (
    CancelDocumentRequest, ConvertQuotationRequest, CreateInvoiceRequest, LineItemInput, PaymentRequest,
    ReversePaymentRequest, UpdateDueDateRequest, UpdateInvoiceRequest, UpdatePaymentRequest,
)
from app.security import get_current_user, require_role
from app.utils.dates import add_months, parse_date, today_local
from app.utils.doc_number import doc_prefix, next_doc_number
from app.utils.gst import compute_invoice_totals, compute_line_gst, compute_lines_with_invoice_discount, get_store_state_code
from app.utils.pagination import Page, paged_response
from app.utils.payments import check_not_overpaid, edit_payment, reverse_payment
from app.utils.logo import load_logo_bytes
from app.utils.signature import load_signature_bytes
from app.utils.pdf import build_invoice_pdf, build_receipt_pdf
from app.utils.upi import upi_payment_uri
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.audit import AuditRoute

router = APIRouter(route_class=AuditRoute, prefix="/invoices", tags=["invoices"], dependencies=[Depends(require_role("OWNER"))])


# An invoice with no due date is due on its own date. Overdue = still owing, not cancelled, and past that day.
_DUE_ON = "COALESCE(i.due_date, i.invoice_date)"
_IS_OVERDUE = f"i.status = 'ACTIVE' AND i.payment_status <> 'PAID' AND {_DUE_ON} < CURRENT_DATE"
_OVERDUE_COLUMNS = (f"{_DUE_ON} AS effective_due_date, "
                    f"CASE WHEN {_IS_OVERDUE} THEN CURRENT_DATE - {_DUE_ON} ELSE 0 END AS days_overdue")


@router.get("")
async def list_invoices(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    payment_status: str | None = None,
    customer_id: str | None = None,
    search: SearchTerm = None,  # invoice no or customer name
    status_filter: str | None = Query(default=None, alias="status", pattern="^(ACTIVE|CANCELLED)$"),
    overdue: bool = False,  # true = only live invoices that are past their due date and not fully paid
    page: Page = Depends(),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT i.*, c.name AS customer_name, c.is_walk_in AS customer_is_walk_in, {_OVERDUE_COLUMNS}, COUNT(*) OVER() AS _total FROM invoices i
               JOIN customers c ON c.id = i.customer_id
               WHERE ($1::date IS NULL OR i.invoice_date >= $1)
                 AND ($2::date IS NULL OR i.invoice_date <= $2)
                 AND ($3::text IS NULL OR i.payment_status = $3)
                 AND ($4::uuid IS NULL OR i.customer_id = $4)
                 AND ($5::text IS NULL OR i.invoice_no ILIKE '%'||$5||'%' ESCAPE '\\' OR c.name ILIKE '%'||$5||'%' ESCAPE '\\'
                      OR i.buyer_name ILIKE '%'||$5||'%' ESCAPE '\\' OR i.buyer_phone ILIKE '%'||$5||'%' ESCAPE '\\')
                 AND ($6::text IS NULL OR i.status = $6)
                 AND (NOT $7::bool OR ({_IS_OVERDUE}))
               ORDER BY i.invoice_date DESC, i.created_at DESC, i.id LIMIT $8 OFFSET $9""",
            parse_date(from_), parse_date(to), payment_status, customer_id, search, status_filter, overdue,
            page.limit, page.offset,
        )
    return paged_response(rows)


async def _refuse_if_credit_notes(conn, invoice_id: str, what: str) -> None:
    """An invoice with live credit notes can't be edited or cancelled: cancel the credit notes first."""
    notes = await conn.fetch("SELECT credit_note_no FROM credit_notes WHERE invoice_id = $1 AND status = 'ACTIVE' ORDER BY credit_note_no", invoice_id)
    if notes:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"This invoice has credit note(s) ({', '.join(n['credit_note_no'] for n in notes)}). "
                                   f"Cancel them before you {what} the invoice.")


async def _fetch_invoice_with_customer(conn, invoice_id: str):
    return await conn.fetchrow(
        f"""SELECT i.*, c.name AS customer_name, c.gstin AS customer_gstin, c.address AS customer_address,
                  c.shipping_address AS customer_shipping_address, c.is_walk_in AS customer_is_walk_in, q.quotation_no, {_OVERDUE_COLUMNS}
           FROM invoices i JOIN customers c ON c.id = i.customer_id
           LEFT JOIN quotations q ON q.id = i.quotation_id
           WHERE i.id = $1""",
        invoice_id,
    )


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        invoice = await _fetch_invoice_with_customer(conn, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        items = await conn.fetch(
            """SELECT ii.*, p.name AS product_name, p.hsn_code AS product_hsn_code,
                      (SELECT array_agg(w.barcode ORDER BY w.barcode) FROM part_warranties w
                        WHERE w.invoice_item_id = ii.id AND w.barcode IS NOT NULL) AS barcodes,
                      (SELECT array_agg(w.barcode ORDER BY w.barcode) FROM part_warranties w
                        WHERE w.invoice_item_id = ii.id AND w.status = 'SOLD' AND w.barcode IS NOT NULL) AS sold_barcodes,
                      (SELECT MAX(w.customer_warranty_months) FROM part_warranties w
                        WHERE w.invoice_item_id = ii.id) AS customer_warranty_months
               FROM invoice_items ii LEFT JOIN products p ON p.id = ii.product_id
               WHERE ii.invoice_id = $1 ORDER BY ii.id""",
            invoice_id,
        )
        tickets = await conn.fetch(
            "SELECT id, ticket_no, title FROM service_tickets WHERE invoice_id = $1 ORDER BY ticket_no", invoice_id
        )
        credit_notes = await conn.fetch(
            """SELECT id, credit_note_no, note_date, total_amount, refund_amount, restock, reason, status
               FROM credit_notes WHERE invoice_id = $1 ORDER BY created_at""", invoice_id)
        returned = {str(r["invoice_item_id"]): r["qty"] for r in await conn.fetch(
            """SELECT ni.invoice_item_id, SUM(ni.quantity) AS qty FROM credit_note_items ni
               JOIN credit_notes n ON n.id = ni.credit_note_id
               WHERE n.invoice_id = $1 AND n.status = 'ACTIVE' GROUP BY ni.invoice_item_id""", invoice_id)}
    result = row_to_dict(invoice)
    result["items"] = rows_to_list(items)
    for row in result["items"]:
        row["returned_quantity"] = returned.get(str(row["id"]), Decimal("0.00"))
    result["credit_notes"] = rows_to_list(credit_notes)
    result["service_tickets"] = rows_to_list(tickets)
    return result


@router.get("/{invoice_id}/pdf")
async def get_invoice_pdf(invoice_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        invoice = await _fetch_invoice_with_customer(conn, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        items = await conn.fetch(
            """SELECT ii.*, p.name AS product_name, p.hsn_code AS product_hsn_code,
                      (SELECT array_agg(w.barcode ORDER BY w.barcode) FROM part_warranties w
                        WHERE w.invoice_item_id = ii.id AND w.barcode IS NOT NULL) AS barcodes
               FROM invoice_items ii
               LEFT JOIN products p ON p.id = ii.product_id WHERE ii.invoice_id = $1 ORDER BY ii.id""",
            invoice_id,
        )
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
        tickets = await conn.fetch(
            "SELECT ticket_no FROM service_tickets WHERE invoice_id = $1 ORDER BY ticket_no", invoice_id
        )
        buyer = await conn.fetchrow(
            "SELECT phone, state, state_code FROM customers WHERE id = $1", invoice["customer_id"]
        )

    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    invoice_data = row_to_dict(invoice)
    if buyer:  # the buyer's contact number and state are printed in the Bill To box
        invoice_data.update(customer_phone=buyer["phone"], customer_state=buyer["state"], customer_state_code=buyer["state_code"])
    # A walk-in sale prints who actually bought, when the shop took a name / number; else it says "Walk-in Customer".
    if invoice["customer_is_walk_in"]:
        if invoice["buyer_name"]:
            invoice_data["customer_name"] = invoice["buyer_name"]
        if invoice["buyer_phone"]:
            invoice_data["customer_phone"] = invoice["buyer_phone"]
    pdf_bytes = await asyncio.to_thread(build_invoice_pdf, store_data, invoice_data, rows_to_list(items), logo, signature,
                                  [t["ticket_no"] for t in tickets])
    safe_name = invoice["invoice_no"].replace("/", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}.pdf"'},
    )


_NOTHING_TO_BILL = {"IN_WARRANTY": "an in-warranty service", "AMC": "covered by an AMC contract"}


async def _bill_service_tickets(conn, invoice, customer_id: str, ticket_ids: list[str], user_id: str) -> None:
    """Marks the given service tickets as billed on `invoice` (call inside the invoice transaction, so any
    problem rolls the whole invoice back). A ticket is billable once it is an attended / closed PAID ticket
    of this customer that isn't on another invoice yet."""
    ids = list(dict.fromkeys(str(i) for i in ticket_ids))
    try:
        ids = [str(uuid.UUID(i)) for i in ids]
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid service ticket id")
    rows = await conn.fetch(
        """SELECT id, ticket_no, customer_id, service_type, status, invoice_id
           FROM service_tickets WHERE id = ANY($1::uuid[]) ORDER BY ticket_no FOR UPDATE""",
        ids,
    )
    if len(rows) != len(ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One of the selected service tickets doesn't exist")
    for t in rows:
        if str(t["customer_id"]) != str(customer_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Ticket {t['ticket_no']} belongs to a different customer")
        if t["service_type"] != "PAID":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Ticket {t['ticket_no']} is {_NOTHING_TO_BILL.get(t['service_type'], 'not a paid service')} — nothing to bill")
        if t["status"] not in ("ATTENDED", "CLOSED"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"Ticket {t['ticket_no']} hasn't been attended yet")
        if t["invoice_id"] is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"Ticket {t['ticket_no']} is already billed on another invoice")
    await conn.execute(
        "UPDATE service_tickets SET invoice_id = $1, updated_at = now() WHERE id = ANY($2::uuid[])",
        invoice["id"], ids,
    )
    for t in rows:
        await conn.execute(
            "INSERT INTO service_ticket_events (ticket_id, event_type, note, actor_id) VALUES ($1,'BILLED',$2,$3)",
            t["id"], f"Billed on invoice {invoice['invoice_no']}", user_id,
        )


async def _ticket_charge_lines(conn, ticket_ids: list[str]) -> list[LineItemInput]:
    """One price line per PAID ticket (its charge_amount and gst_rate), for `include_ticket_charges`. Locks the tickets,
    so the charge can't be edited between reading it here and billing it. Other problems (wrong customer, not attended,
    already billed) are reported by `_bill_service_tickets` right after."""
    ids = list(dict.fromkeys(str(i) for i in ticket_ids))
    try:
        ids = [str(uuid.UUID(i)) for i in ids]
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid service ticket id")
    rows = await conn.fetch(
        """SELECT id, ticket_no, title, service_type, charge_amount, gst_rate
           FROM service_tickets WHERE id = ANY($1::uuid[]) ORDER BY ticket_no FOR UPDATE""",
        ids,
    )
    if len(rows) != len(ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One of the selected service tickets doesn't exist")
    lines: list[LineItemInput] = []
    for t in rows:
        if t["service_type"] != "PAID":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Ticket {t['ticket_no']} is {_NOTHING_TO_BILL.get(t['service_type'], 'not a paid service')} — nothing to bill")
        if t["charge_amount"] is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ticket {t['ticket_no']} has no charge amount. Set it on the ticket, or send the price line yourself",
            )
        lines.append(LineItemInput(
            description=f"Service {t['ticket_no']}: {t['title']}"[:2000],
            quantity=Decimal("1"), unit_price=t["charge_amount"], gst_rate=t["gst_rate"],
        ))
    return lines


async def _check_stock(conn, items: list[LineItemInput], allow_negative: bool) -> list[str]:
    """Stops a sale taking stock below zero. Lines that name their barcodes are skipped: each of those
    units is checked one by one (must be IN_STOCK) further down. Products are locked in id order, so two
    simultaneous sales can't both pass on the same last unit. Returns warnings when `allow_negative`."""
    needed: dict[str, Decimal] = {}
    for item in items:
        if item.product_id is None:
            continue
        try:
            product_id = str(uuid.UUID(str(item.product_id)))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid product id")
        item.product_id = product_id
        if not item.barcodes:
            needed[product_id] = needed.get(product_id, Decimal("0")) + item.quantity
    ids = sorted({i.product_id for i in items if i.product_id})
    if not ids:
        return []
    rows = await conn.fetch(
        "SELECT id, name, current_stock FROM products WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE", ids
    )
    products = {str(r["id"]): r for r in rows}
    if len(products) != len(ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One of the products doesn't exist")

    short = []
    for product_id, qty in needed.items():
        have = products[product_id]["current_stock"]
        if qty > have:
            short.append(f"{products[product_id]['name']} (in stock {have:g}, needed {qty:g})")
    if not short:
        return []
    if not allow_negative:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Not enough stock for: " + "; ".join(short) + ". Record the purchase first, or confirm selling anyway.",
        )
    return [f"Stock went negative: {line}" for line in short]


async def _check_credit_limit(conn, customer_id: str, new_total, allow_over: bool) -> None:
    """Refuses (409) an invoice that would take the customer's outstanding balance (opening balance + unpaid live
    invoices) over their credit limit, unless the owner confirmed. Call inside the invoice transaction: the customer row
    is locked, so two invoices made at the same moment can't both slip under the limit."""
    row = await conn.fetchrow(
        """SELECT c.credit_limit, COALESCE(c.opening_balance, 0) AS opening_balance,
                  (SELECT COALESCE(SUM(i.total_amount - i.paid_amount), 0) FROM invoices i
                    WHERE i.customer_id = c.id AND i.status = 'ACTIVE') AS invoice_due
           FROM customers c WHERE c.id = $1 FOR NO KEY UPDATE""",
        customer_id,
    )
    if row is None or row["credit_limit"] is None or allow_over:
        return
    outstanding = row["opening_balance"] + row["invoice_due"]
    if outstanding + new_total > row["credit_limit"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"This invoice (Rs. {new_total:.2f}) would take the customer to Rs. {outstanding + new_total:.2f} "
                    f"owed, over their credit limit of Rs. {row['credit_limit']:.2f} (they owe Rs. {outstanding:.2f} now). "
                    f"Collect a payment first, or confirm to invoice anyway."),
        )


async def _insert_invoice_lines(conn, invoice, computed, user_id) -> tuple[list[dict], list[str]]:
    """Writes the lines of an invoice (call inside the invoice transaction): the line rows, the SALE stock-ledger rows,
    and, for lines that name barcodes, flips each unit to SOLD on this invoice with its customer warranty. `computed` is
    a list of (LineItemInput, LineGst). Shared by creating and editing an invoice. Returns (lines, warnings)."""
    result_items = []
    warnings: list[str] = []
    product_hsn = {
        str(r["id"]): r["hsn_code"]
        for r in await conn.fetch("SELECT id, hsn_code FROM products WHERE id = ANY($1::uuid[])",
                                  [i.product_id for i, _ in computed if i.product_id])
    }
    for item, gst in computed:
        # The code is copied onto the line now, so changing the product's HSN later can't alter this invoice.
        line_hsn = item.hsn_code or (product_hsn.get(str(item.product_id)) if item.product_id else None)
        invoice_item = await conn.fetchrow(
            """INSERT INTO invoice_items (invoice_id, product_id, description, quantity, unit_price, gst_rate, cgst_amount, sgst_amount, igst_amount, line_total, hsn_code,
                                       discount_percent, discount_amount, cost_price)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING id""",
            invoice["id"], item.product_id, item.description, item.quantity, item.unit_price,
            item.gst_rate, gst.cgst, gst.sgst, gst.igst, gst.line_total, line_hsn,
            item.discount_percent, gst.discount_amount, None if item.product_id else item.cost_price,
        )
        if item.product_id:
            await conn.execute(
                """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, created_by)
                   VALUES ($1,$2,'SALE',$3,$4)""",
                item.product_id, -abs(item.quantity), invoice["id"], user_id,
            )

        if item.barcodes:
            if item.quantity != item.quantity.to_integral_value() or len(item.barcodes) != int(item.quantity):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Line for product {item.product_id} has quantity {item.quantity} "
                           f"but {len(item.barcodes)} barcode(s) were given — they must match 1:1",
                )
            months = item.customer_warranty_months
            valid_until = add_months(invoice["invoice_date"], months) if months else None
            for barcode in item.barcodes:
                # Lock the row so two simultaneous sales can't both grab the same unit.
                unit = await conn.fetchrow(
                    "SELECT * FROM part_warranties WHERE product_id = $1 AND barcode = $2 FOR UPDATE",
                    item.product_id, barcode,
                )
                if unit is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Barcode '{barcode}' isn't a tracked unit for this product — record its purchase first",
                    )
                if unit["status"] != "IN_STOCK":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Barcode '{barcode}' is not available to sell (status: {unit['status']})",
                    )
                await conn.execute(
                    """UPDATE part_warranties SET status='SOLD', invoice_id=$1, invoice_item_id=$2,
                              sale_date=$3, customer_warranty_months=$4, customer_warranty_valid_until=$5,
                              updated_at=now() WHERE id=$6""",
                    invoice["id"], invoice_item["id"], invoice["invoice_date"], months, valid_until, unit["id"],
                )
        elif item.product_id:
            product = await conn.fetchrow("SELECT name, is_serialized FROM products WHERE id = $1", item.product_id)
            if product and product["is_serialized"]:
                warnings.append(
                    f"{product['name']}: sold without serial numbers, so no warranty is linked to this line"
                )

        result_items.append({**item.model_dump(), "cgst": gst.cgst, "sgst": gst.sgst,
                              "igst": gst.igst, "line_total": gst.line_total,
                              "discount_amount": gst.discount_amount})

    return result_items, warnings


async def _create_invoice(conn, customer_id: str, invoice_type: str, quotation_id: str | None,
                           notes: str | None, items: list[LineItemInput], user_id: str,
                           service_ticket_ids: list[str] | None = None, allow_negative_stock: bool = False,
                           invoice_discount_percent=0, invoice_discount_amount=0,
                           billing_address: str | None = None, shipping_address: str | None = None,
                           include_ticket_charges: bool = False, payment_terms_days: int | None = None,
                           due_date=None, allow_over_credit_limit: bool = False,
                           buyer_name: str | None = None, buyer_phone: str | None = None) -> dict:
    customer = await conn.fetchrow(
        "SELECT state_code, address, shipping_address, payment_terms_days, is_walk_in FROM customers WHERE id = $1", customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    buyer_name = (buyer_name or "").strip() or None
    buyer_phone = (buyer_phone or "").strip() or None
    if (buyer_name or buyer_phone) and not customer["is_walk_in"]:
        raise HTTPException(status_code=422,
                            detail="buyer_name / buyer_phone are only for the walk-in customer")
    buyer_state_code = customer["state_code"]
    # Each invoice keeps its own copy of both addresses. Not given (None) = the customer's saved ones;
    # given as "" = none (shipping "" means "same as billing").
    invoice_billing = (customer["address"] if billing_address is None else billing_address) or None
    invoice_shipping = (customer["shipping_address"] if shipping_address is None else shipping_address) or None
    invoice_billing = invoice_billing.strip() if invoice_billing else None
    invoice_shipping = invoice_shipping.strip() if invoice_shipping else None
    store_state_code = await get_store_state_code(conn)
    is_interstate = bool(buyer_state_code and buyer_state_code != store_state_code)
    # Due date: a date wins, then terms given on this invoice, then the customer's default terms; none = due on the
    # invoice date. (The date itself is worked out in SQL from CURRENT_DATE, the same day the invoice gets.)
    terms = None if due_date is not None else (payment_terms_days if payment_terms_days is not None
                                               else customer["payment_terms_days"])

    async with conn.transaction():
        # First thing in the transaction: waits for any other invoice being created, then no row locks are held
        # while waiting (see next_doc_number).
        invoice_no = await next_doc_number(conn, "invoices", "invoice_no", await doc_prefix(conn, "invoice_prefix", "INV"))

        if quotation_id:
            # Lock the quotation so two taps of "Convert" can't create two invoices from it.
            quote = await conn.fetchrow("SELECT status FROM quotations WHERE id = $1 FOR UPDATE", quotation_id)
            if quote is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quotation not found")
            if quote["status"] == "CONVERTED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This quotation was already converted to an invoice")
            if quote["status"] == "REJECTED":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A rejected quotation can't be converted")

        if include_ticket_charges:
            items = await _ticket_charge_lines(conn, service_ticket_ids or []) + list(items)
        if not items:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one item required")

        stock_warnings = await _check_stock(conn, items, allow_negative_stock)

        try:
            lines, invoice_discount = compute_lines_with_invoice_discount(
                [(i.quantity, i.unit_price, i.gst_rate, i.discount_percent) for i in items],
                buyer_state_code, store_state_code, invoice_discount_percent, invoice_discount_amount)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        computed = list(zip(items, lines))
        totals = compute_invoice_totals([gst for _, gst in computed])
        await _check_credit_limit(conn, customer_id, totals.total_amount, allow_over_credit_limit)

        invoice = await conn.fetchrow(
            """INSERT INTO invoices (invoice_no, customer_id, quotation_id, place_of_supply_state_code, is_interstate,
                                      subtotal, total_cgst, total_sgst, total_igst, round_off, total_amount, invoice_type, notes, created_by, total_discount,
                                      invoice_discount_percent, invoice_discount_amount, billing_address, shipping_address,
                                      payment_terms_days, due_date, buyer_name, buyer_phone)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
                       COALESCE($21::date, CASE WHEN $20::int IS NOT NULL THEN CURRENT_DATE + $20::int END), $22, $23) RETURNING *""",
            invoice_no, customer_id, quotation_id, buyer_state_code, is_interstate,
            totals.subtotal, totals.total_cgst, totals.total_sgst, totals.total_igst,
            totals.round_off, totals.total_amount, invoice_type, notes, user_id, totals.total_discount,
            invoice_discount_percent if not invoice_discount_amount else 0, invoice_discount,
            invoice_billing, invoice_shipping, terms, due_date, buyer_name, buyer_phone,
        )

        if service_ticket_ids:
            await _bill_service_tickets(conn, invoice, customer_id, service_ticket_ids, user_id)

        result_items, line_warnings = await _insert_invoice_lines(conn, invoice, computed, user_id)
        warnings = list(stock_warnings) + line_warnings

        if quotation_id:
            await conn.execute(
                "UPDATE quotations SET status = 'CONVERTED', converted_invoice_id = $1 WHERE id = $2",
                invoice["id"], quotation_id,
            )

    result = row_to_dict(invoice)
    result["items"] = result_items
    result["warnings"] = warnings
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_invoice(body: CreateInvoiceRequest, current_user=Depends(get_current_user)):
    if not body.items and not (body.include_ticket_charges and body.service_ticket_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one item required")
    if body.include_ticket_charges and not body.service_ticket_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="include_ticket_charges needs service_ticket_ids")
    if body.service_ticket_ids:
        if current_user["role"] != "OWNER":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can bill service tickets")
        if body.invoice_type != "SERVICE":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Service tickets can only be billed on a SERVICE invoice")
    pool = get_pool()
    async with pool.acquire() as conn:
        return await _create_invoice(
            conn, body.customer_id, body.invoice_type, body.quotation_id, body.notes, body.items, current_user["id"],
            body.service_ticket_ids, body.allow_negative_stock,
            body.invoice_discount_percent, body.invoice_discount_amount,
            body.billing_address, body.shipping_address, body.include_ticket_charges,
            body.payment_terms_days, body.due_date, body.allow_over_credit_limit,
            buyer_name=body.buyer_name, buyer_phone=body.buyer_phone,
        )


@router.post("/from-quotation/{quotation_id}", status_code=status.HTTP_201_CREATED)
async def create_invoice_from_quotation(
    quotation_id: str,
    body: ConvertQuotationRequest | None = None,
    current_user=Depends(get_current_user),
):
    """Convert a quotation into an invoice. For serialized lines, pass the physical barcodes (and
    optionally the warranty months) per quotation line in `items` so each unit is linked to its
    warranty exactly like a directly-created invoice. Without a body it converts as before."""
    body = body or ConvertQuotationRequest()
    pool = get_pool()
    async with pool.acquire() as conn:
        quotation = await conn.fetchrow("SELECT * FROM quotations WHERE id = $1", quotation_id)
        if quotation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quotation not found")

        q_items = await conn.fetch("SELECT * FROM quotation_items WHERE quotation_id = $1 ORDER BY id", quotation_id)
        overrides = {o.quotation_item_id: o for o in body.items}
        unknown = set(overrides) - {str(it["id"]) for it in q_items}
        if unknown:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="items refers to a line that isn't on this quotation")

        items = []
        for it in q_items:
            override = overrides.get(str(it["id"]))
            months = (override.customer_warranty_months if override and override.customer_warranty_months is not None
                      else it["customer_warranty_months"])
            items.append(
                LineItemInput(
                    product_id=str(it["product_id"]) if it["product_id"] else None,
                    description=it["description"],
                    quantity=it["quantity"],
                    unit_price=it["unit_price"],
                    gst_rate=it["gst_rate"],
                    discount_percent=it["discount_percent"],
                    barcodes=(override.barcodes or None) if override else None,
                    customer_warranty_months=months,
                )
            )

        return await _create_invoice(
            conn, str(quotation["customer_id"]), "SALE", str(quotation["id"]),
            body.notes if body.notes is not None else quotation["notes"], items, current_user["id"],
            allow_negative_stock=body.allow_negative_stock,
            # The quotation's discount carries over: the percent if it was entered as one, else the flat amount.
            invoice_discount_percent=quotation["invoice_discount_percent"],
            invoice_discount_amount=0 if quotation["invoice_discount_percent"] > 0 else quotation["invoice_discount_amount"],
            billing_address=body.billing_address, shipping_address=body.shipping_address,
            payment_terms_days=body.payment_terms_days, due_date=body.due_date,
            allow_over_credit_limit=body.allow_over_credit_limit,
        )


@router.get("/{invoice_id}/payments")
async def list_invoice_payments(invoice_id: str, current_user=Depends(get_current_user)):
    """Payment history (newest first) for one invoice. The platform admin's name is hidden from
    everyone except the platform admin itself, same as everywhere else it could otherwise leak."""
    pool = get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM invoices WHERE id = $1", invoice_id)
        if not exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        rows = await conn.fetch(
            """SELECT p.id, p.amount, p.payment_mode, p.payment_date, p.reference_no, p.notes, p.created_at,
                      p.reversed_at, p.reversal_reason, p.reverses_payment_id, p.credit_note_id, p.debit_note_id,
                      CASE WHEN u.is_platform_admin AND u.id <> $2::uuid THEN NULL ELSE u.name END AS created_by_name
               FROM payments p LEFT JOIN users u ON u.id = p.created_by
               WHERE p.invoice_id = $1 ORDER BY p.payment_date DESC, p.created_at DESC, p.id""",
            invoice_id, current_user["id"],
        )
    return rows_to_list(rows)


@router.post("/{invoice_id}/payments")
async def add_invoice_payment(invoice_id: str, body: PaymentRequest, current_user=Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT id, status, total_amount, paid_amount FROM invoices WHERE id = $1 FOR UPDATE", invoice_id
            )
            if locked is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if locked["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invoice is cancelled — payments can't be added")
            check_not_overpaid(body.amount, locked["total_amount"], locked["paid_amount"], "invoice")
            await conn.execute(
                """INSERT INTO payments (invoice_id, amount, payment_mode, payment_date, reference_no, notes, created_by)
                   VALUES ($1,$2,$3,COALESCE($4,CURRENT_DATE),$5,$6,$7)""",
                invoice_id, body.amount, body.payment_mode, body.payment_date,
                body.reference_no, body.notes, current_user["id"],
            )
            row = await conn.fetchrow(
                """UPDATE invoices SET paid_amount = paid_amount + $1,
                          payment_status = CASE WHEN paid_amount + $1 >= total_amount THEN 'PAID'
                                                 WHEN paid_amount + $1 > 0 THEN 'PARTIAL'
                                                 ELSE 'UNPAID' END
                   WHERE id = $2 RETURNING *""",
                body.amount, invoice_id,
            )
    return row_to_dict(row)


@router.put("/{invoice_id}/payments/{payment_id}")
async def update_invoice_payment(invoice_id: str, payment_id: str, body: UpdatePaymentRequest):
    """Fix the mode / date / reference / notes of a payment. The amount can't be edited: reverse the payment
    (POST .../reverse) and record the right one."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await edit_payment(conn, "invoice", invoice_id, payment_id,
                                 {k: getattr(body, k) for k in body.model_fields_set})
    return row_to_dict(row)


@router.post("/{invoice_id}/payments/{payment_id}/reverse")
async def reverse_invoice_payment(invoice_id: str, payment_id: str, body: ReversePaymentRequest,
                                  owner=Depends(require_role("OWNER"))):
    """OWNER only. Undoes a wrongly entered payment without deleting it: a negative payment is booked against the
    original (which is marked reversed with the reason), and the invoice's paid amount / status are recalculated.
    Refused (409) on a cancelled invoice and on a payment that was already reversed. Returns the updated invoice
    plus `reversal_id`."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await reverse_payment(conn, "invoice", invoice_id, payment_id, body.reason.strip(), owner["id"])
    invoice = row_to_dict(result["document"])
    invoice["reversal_id"] = result["reversal_id"]
    return invoice


@router.get("/{invoice_id}/payments/{payment_id}/receipt")
async def get_payment_receipt(invoice_id: str, payment_id: str):
    """PDF receipt for one payment received on an invoice (not for a refund or a reversed payment)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("SELECT * FROM payments WHERE id = $1 AND invoice_id = $2", payment_id, invoice_id)
        if payment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
        if payment["amount"] <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A receipt can only be made for a payment received")
        if payment["reversed_at"] is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This payment was reversed, so it has no receipt")
        invoice = await _fetch_invoice_with_customer(conn, invoice_id)
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    pdf_bytes = await asyncio.to_thread(build_receipt_pdf, 
        store_data, "invoice", row_to_dict(invoice), row_to_dict(payment),
        {"name": invoice["customer_name"], "gstin": invoice["customer_gstin"]}, logo, signature)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="receipt-{str(payment["id"])[:8]}.pdf"'},
    )


@router.get("/{invoice_id}/upi-link")
async def get_invoice_upi_link(invoice_id: str):
    """The `upi://pay` link for what is still owed on this invoice (the same one the PDF's QR code carries), so the
    app can open a UPI app or share it by message. 400 when the shop has no UPI ID saved or nothing is owed; 409 on
    a cancelled invoice."""
    pool = get_pool()
    async with pool.acquire() as conn:
        invoice = await conn.fetchrow("SELECT invoice_no, status, total_amount, paid_amount FROM invoices WHERE id = $1", invoice_id)
        if invoice is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        store = await conn.fetchrow("SELECT store_name, upi_id FROM store_profile WHERE id = 1")
    if invoice["status"] == "CANCELLED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invoice is cancelled")
    if not store or not store["upi_id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Save the shop's UPI ID in the store profile first")
    balance = invoice["total_amount"] - invoice["paid_amount"]
    if balance <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nothing is owed on this invoice")
    return {"upi_id": store["upi_id"], "amount": balance,
            "upi_uri": upi_payment_uri(store["upi_id"], store["store_name"], balance, invoice["invoice_no"])}


@router.put("/{invoice_id}/due-date")
async def update_invoice_due_date(invoice_id: str, body: UpdateDueDateRequest):
    """Change when an invoice is due: `payment_terms_days` (counted from the invoice date) or an explicit `due_date`;
    both null removes the due date (the invoice is then due on its own date)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            invoice = await conn.fetchrow("SELECT id, status, invoice_date FROM invoices WHERE id = $1 FOR UPDATE", invoice_id)
            if invoice is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if invoice["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invoice is cancelled")
            if body.due_date is not None and body.due_date < invoice["invoice_date"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The due date can't be before the invoice date")
            row = await conn.fetchrow(
                """UPDATE invoices SET payment_terms_days = $2,
                          due_date = COALESCE($3::date, CASE WHEN $2::int IS NOT NULL THEN invoice_date + $2::int END)
                   WHERE id = $1 RETURNING *""",
                invoice_id, body.payment_terms_days, body.due_date,
            )
    return row_to_dict(row)


_RELEASE_UNITS_SQL = """UPDATE part_warranties SET status = 'IN_STOCK', invoice_id = NULL, invoice_item_id = NULL,
                               sale_date = NULL, customer_warranty_months = NULL, customer_warranty_valid_until = NULL,
                               updated_at = now()
                        WHERE invoice_id = $1"""


def _uuid_str(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid product id")


@router.put("/{invoice_id}")
async def update_invoice(invoice_id: str, body: UpdateInvoiceRequest, owner=Depends(require_role("OWNER"))):
    """OWNER only. Edits a live invoice in place: same id, same invoice number, same customer.

    `items` replaces all the lines. In one transaction the stock this invoice took goes back (a RETURN_IN row, so the
    history stays), its serial-numbered units are released, and the new lines are sold exactly as when making an invoice
    (stock check, barcodes, GST, discounts, totals). Notes, dates, addresses and discount can be changed too (see
    UpdateInvoiceRequest). Payments received stay: the status is recalculated, and an edit that would bring the total
    below what was already received is refused (409) until a payment is reversed.

    Refused (409) on a cancelled invoice, while a unit on it has a warranty claim / replacement, when a serial number is
    taken off the invoice but has service tickets, and for stock / credit limit unless confirmed (like making one).
    Returns the updated invoice (as GET /invoices/{id}) plus `warnings`."""
    sent = body.model_fields_set
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            invoice = await conn.fetchrow("SELECT * FROM invoices WHERE id = $1 FOR UPDATE", invoice_id)
            if invoice is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if invoice["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A cancelled invoice can't be edited")
            await _refuse_if_credit_notes(conn, invoice_id, "edit")

            old_date = invoice["invoice_date"]
            new_date = body.invoice_date or old_date
            if new_date != old_date and new_date > today_local():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The invoice date can't be in the future")

            # Due date: sent = what was sent wins (terms count from the invoice date); not sent = keep, but terms follow a moved date.
            terms, due_date = invoice["payment_terms_days"], invoice["due_date"]
            if "payment_terms_days" in sent or "due_date" in sent:
                terms, due_date = body.payment_terms_days, body.due_date
                if due_date is None and terms is not None:
                    due_date = new_date + timedelta(days=terms)
            elif new_date != old_date and terms is not None:
                due_date = new_date + timedelta(days=terms)
            if due_date is not None and due_date < new_date:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The due date can't be before the invoice date")

            billing = (body.billing_address or "").strip() or None if "billing_address" in sent else invoice["billing_address"]
            shipping = (body.shipping_address or "").strip() or None if "shipping_address" in sent else invoice["shipping_address"]
            notes = (body.notes or "").strip() or None if "notes" in sent else invoice["notes"]
            buyer_name, buyer_phone = invoice["buyer_name"], invoice["buyer_phone"]
            if "buyer_name" in sent or "buyer_phone" in sent:
                if not await conn.fetchval("SELECT is_walk_in FROM customers WHERE id = $1", invoice["customer_id"]):
                    raise HTTPException(status_code=422,
                                        detail="buyer_name / buyer_phone are only for walk-in invoices")
                if "buyer_name" in sent:
                    buyer_name = (body.buyer_name or "").strip() or None
                if "buyer_phone" in sent:
                    buyer_phone = (body.buyer_phone or "").strip() or None

            # Lock every product involved first (in id order, like making an invoice), then the units.
            old_products = await conn.fetch(
                "SELECT DISTINCT product_id FROM stock_ledger WHERE reference_id = $1 AND reference_type IN ('SALE', 'RETURN_IN')",
                invoice_id,
            )
            new_products = [_uuid_str(i.product_id) for i in body.items if i.product_id]
            lock_ids = sorted({str(r["product_id"]) for r in old_products} | set(new_products))
            if lock_ids:
                await conn.fetch("SELECT id FROM products WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE", lock_ids)

            units = await conn.fetch(
                "SELECT id, product_id, barcode, status FROM part_warranties WHERE invoice_id = $1 FOR UPDATE", invoice_id)
            busy = [u for u in units if u["status"] != "SOLD"]
            if busy:
                names = ", ".join(sorted({u["barcode"] or str(u["id"])[:8] for u in busy}))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A warranty claim or replacement was recorded on: {names}. Resolve it before editing.",
                )
            kept = {(_uuid_str(i.product_id), b) for i in body.items if i.product_id for b in (i.barcodes or [])}
            removed = [u for u in units if (str(u["product_id"]), u["barcode"]) not in kept]
            if removed:
                with_tickets = await conn.fetch(
                    """SELECT DISTINCT w.barcode, w.id FROM service_tickets t JOIN part_warranties w ON w.id = t.warranty_id
                       WHERE t.warranty_id = ANY($1::uuid[])""",
                    [u["id"] for u in removed],
                )
                if with_tickets:
                    names = ", ".join(sorted({r["barcode"] or str(r["id"])[:8] for r in with_tickets}))
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Service tickets exist for {names}, so it can't be taken off this invoice. "
                               f"Cancel the invoice instead.",
                    )

            # Undo what the old lines did: stock back in, units back in stock, old lines gone.
            await conn.execute(
                """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                   SELECT product_id, -SUM(quantity), 'RETURN_IN', reference_id, $2::text, $3::uuid
                   FROM stock_ledger WHERE reference_type IN ('SALE', 'RETURN_IN') AND reference_id = $1
                   GROUP BY product_id, reference_id HAVING SUM(quantity) <> 0""",
                invoice_id, f"Invoice {invoice['invoice_no']} edited", owner["id"],
            )
            await conn.execute(_RELEASE_UNITS_SQL, invoice_id)
            await conn.execute("DELETE FROM invoice_items WHERE invoice_id = $1", invoice_id)

            items = list(body.items)
            stock_warnings = await _check_stock(conn, items, body.allow_negative_stock)

            buyer_state_code = invoice["place_of_supply_state_code"]
            store_state_code = await get_store_state_code(conn)
            if "invoice_discount_percent" in sent or "invoice_discount_amount" in sent:
                discount_percent, discount_amount = body.invoice_discount_percent, body.invoice_discount_amount
            else:  # unchanged: the percent if it was entered as one, else the flat amount
                discount_percent = invoice["invoice_discount_percent"]
                discount_amount = Decimal("0") if discount_percent > 0 else invoice["invoice_discount_amount"]
            try:
                lines, invoice_discount = compute_lines_with_invoice_discount(
                    [(i.quantity, i.unit_price, i.gst_rate, i.discount_percent) for i in items],
                    buyer_state_code, store_state_code, discount_percent, discount_amount)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
            computed = list(zip(items, lines))
            totals = compute_invoice_totals([gst for _, gst in computed])

            paid = invoice["paid_amount"]
            if totals.total_amount < paid:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Rs. {paid:.2f} has already been received on this invoice, which is more than the edited total "
                           f"of Rs. {totals.total_amount:.2f}. Reverse or correct a payment first.",
                )
            increase = totals.total_amount - invoice["total_amount"]
            if increase > 0:  # only a bigger invoice can take the customer over their limit
                await _check_credit_limit(conn, str(invoice["customer_id"]), increase, body.allow_over_credit_limit)

            updated = await conn.fetchrow(
                """UPDATE invoices SET invoice_date = $2, is_interstate = $3, subtotal = $4, total_cgst = $5, total_sgst = $6,
                          total_igst = $7, round_off = $8, total_amount = $9::numeric, total_discount = $10,
                          invoice_discount_percent = $11, invoice_discount_amount = $12, notes = $13,
                          billing_address = $14, shipping_address = $15, payment_terms_days = $16, due_date = $17,
                          buyer_name = $18, buyer_phone = $19,
                          payment_status = CASE WHEN paid_amount >= $9::numeric THEN 'PAID'
                                                WHEN paid_amount > 0 THEN 'PARTIAL' ELSE 'UNPAID' END
                   WHERE id = $1 RETURNING *""",
                invoice_id, new_date, bool(buyer_state_code and buyer_state_code != store_state_code),
                totals.subtotal, totals.total_cgst, totals.total_sgst, totals.total_igst, totals.round_off,
                totals.total_amount, totals.total_discount,
                discount_percent if not discount_amount else 0, invoice_discount,
                notes, billing, shipping, terms, due_date, buyer_name, buyer_phone,
            )
            _, line_warnings = await _insert_invoice_lines(conn, updated, computed, owner["id"])

    result = await get_invoice(invoice_id)
    result["warnings"] = list(stock_warnings) + line_warnings
    return result


@router.post("/{invoice_id}/cancel")
async def cancel_invoice(invoice_id: str, body: CancelDocumentRequest, owner=Depends(require_role("OWNER"))):
    """OWNER only. Cancels an invoice but keeps it on file (the number is never reused). Everything it
    did is undone in one transaction: the stock it took out goes back, each serialized unit goes back
    to IN_STOCK with its customer warranty cleared, service tickets it billed become billable again,
    pending service tickets raised against its units are cancelled (`cancelled_tickets` lists their numbers),
    and a converted quotation is re-opened (ACCEPTED). Cancelled invoices drop out of reports,
    customer balances and the dashboard.

    Refused (409) while a unit from the invoice is claimed / replaced, and while money is recorded
    against it unless `refund_payments` is true (then a negative payment books the refund)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            invoice = await conn.fetchrow("SELECT * FROM invoices WHERE id = $1 FOR UPDATE", invoice_id)
            if invoice is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if invoice["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invoice is already cancelled")
            await _refuse_if_credit_notes(conn, invoice_id, "cancel")

            paid = invoice["paid_amount"]
            if paid > 0 and not body.refund_payments:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Rs. {paid:.2f} has been received on this invoice. Refund it to the customer, "
                           f"then cancel again and confirm the refund.",
                )

            units = await conn.fetch("SELECT id, barcode, status FROM part_warranties WHERE invoice_id = $1 FOR UPDATE", invoice_id)
            busy = [u for u in units if u["status"] != "SOLD"]
            if busy:
                names = ", ".join(sorted({u["barcode"] or str(u["id"])[:8] for u in busy}))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A warranty claim or replacement was recorded on: {names}. Resolve it before cancelling.",
                )

            if paid > 0:
                last_mode = await conn.fetchval(
                    "SELECT payment_mode FROM payments WHERE invoice_id = $1 ORDER BY created_at DESC LIMIT 1", invoice_id
                )
                await conn.execute(
                    """INSERT INTO payments (invoice_id, amount, payment_mode, payment_date, notes, created_by)
                       VALUES ($1, $2, $3, CURRENT_DATE, $4, $5)""",
                    invoice_id, -paid, last_mode or "CASH", f"Refund on cancellation of {invoice['invoice_no']}", owner["id"],
                )

            # Stock: put back whatever this invoice still has out (its SALE rows net of any edit's RETURN_IN rows).
            await conn.execute(
                """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                   SELECT product_id, -SUM(quantity), 'RETURN_IN', reference_id, $2::text, $3::uuid
                   FROM stock_ledger WHERE reference_type IN ('SALE', 'RETURN_IN') AND reference_id = $1
                   GROUP BY product_id, reference_id HAVING SUM(quantity) <> 0""",
                invoice_id, f"Invoice {invoice['invoice_no']} cancelled", owner["id"],
            )
            # Service tickets raised against a unit sold on this invoice go with it: once the sale is void the unit
            # isn't the customer's any more. Only work still pending is cancelled (OPEN / RESCHEDULED / ATTENDED);
            # a CLOSED ticket stays as history, and one already billed on a different invoice is left alone.
            cancelled_tickets = await conn.fetch(
                """UPDATE service_tickets SET status = 'CANCELLED', updated_at = now()
                   WHERE warranty_id = ANY($1::uuid[]) AND status IN ('OPEN', 'RESCHEDULED', 'ATTENDED')
                     AND (invoice_id IS NULL OR invoice_id = $2)
                   RETURNING id, ticket_no""",
                [u["id"] for u in units], invoice_id,
            )
            for t in cancelled_tickets:
                await conn.execute(
                    "INSERT INTO service_ticket_events (ticket_id, event_type, note, actor_id) VALUES ($1,'CANCELLED',$2,$3)",
                    t["id"], f"Invoice {invoice['invoice_no']} cancelled — the unit is no longer with the customer", owner["id"],
                )
            await conn.execute(
                """UPDATE part_warranties SET status = 'IN_STOCK', invoice_id = NULL, invoice_item_id = NULL,
                          sale_date = NULL, customer_warranty_months = NULL, customer_warranty_valid_until = NULL,
                          updated_at = now()
                   WHERE invoice_id = $1""",
                invoice_id,
            )
            unbilled = await conn.fetch(
                "UPDATE service_tickets SET invoice_id = NULL, updated_at = now() WHERE invoice_id = $1 RETURNING id", invoice_id
            )
            for t in unbilled:
                await conn.execute(
                    "INSERT INTO service_ticket_events (ticket_id, event_type, note, actor_id) VALUES ($1,'UNBILLED',$2,$3)",
                    t["id"], f"Invoice {invoice['invoice_no']} cancelled — billable again", owner["id"],
                )
            await conn.execute("UPDATE amc_contracts SET linked_invoice_id = NULL WHERE linked_invoice_id = $1", invoice_id)
            if invoice["quotation_id"]:
                await conn.execute(
                    """UPDATE quotations SET status = 'ACCEPTED', converted_invoice_id = NULL
                       WHERE id = $1 AND converted_invoice_id = $2""",
                    invoice["quotation_id"], invoice_id,
                )

            row = await conn.fetchrow(
                """UPDATE invoices SET status = 'CANCELLED', cancelled_at = now(), cancel_reason = $2, cancelled_by = $3,
                          paid_amount = 0,
                          payment_status = CASE WHEN paid_amount > 0 THEN 'UNPAID' ELSE payment_status END
                   WHERE id = $1 RETURNING *""",
                invoice_id, body.reason.strip(), owner["id"],
            )
    result = row_to_dict(row)
    result["cancelled_tickets"] = [t["ticket_no"] for t in cancelled_tickets]
    return result


@router.delete("/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cancelled_invoice(invoice_id: str, owner=Depends(require_role("OWNER"))):
    """OWNER only. Permanently deletes an invoice that is already CANCELLED (409 on a live one: cancel it first, which
    puts back its stock, units and tickets). The invoice, its lines and its payment rows (the payments and the refund
    booked when it was cancelled) are removed; the stock-ledger rows stay (they net to zero) and the audit log keeps a
    record of who deleted which invoice number. Note that deleting the newest invoice frees its number for the next one."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            invoice = await conn.fetchrow("SELECT id, invoice_no, status FROM invoices WHERE id = $1 FOR UPDATE", invoice_id)
            if invoice is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if invoice["status"] != "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail="Only a cancelled invoice can be deleted. Cancel it first.")
            # Cancelling already cleared these; this only makes sure nothing can still point at the invoice.
            await conn.execute("UPDATE service_tickets SET invoice_id = NULL WHERE invoice_id = $1", invoice_id)
            await conn.execute("UPDATE amc_contracts SET linked_invoice_id = NULL WHERE linked_invoice_id = $1", invoice_id)
            await conn.execute("UPDATE quotations SET converted_invoice_id = NULL WHERE converted_invoice_id = $1", invoice_id)
            await conn.execute("UPDATE part_warranties SET invoice_id = NULL, invoice_item_id = NULL WHERE invoice_id = $1", invoice_id)
            await conn.execute("DELETE FROM payments WHERE invoice_id = $1", invoice_id)
            await conn.execute("DELETE FROM credit_notes WHERE invoice_id = $1", invoice_id)  # only cancelled ones can be left
            await conn.execute("DELETE FROM invoices WHERE id = $1", invoice_id)  # its lines go with it (ON DELETE CASCADE)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
