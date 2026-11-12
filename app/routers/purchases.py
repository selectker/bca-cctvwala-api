import asyncio
from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.billing import (
    CancelDocumentRequest, CreatePurchaseRequest, PaymentRequest, ReversePaymentRequest, UpdatePaymentRequest,
    UpdatePurchaseRequest,
)
from app.security import get_current_user, require_role
from app.utils.dates import add_months, parse_date
from app.utils.doc_number import next_doc_number
from app.utils.gst import MAX_DOCUMENT_TOTAL, AmountTooLarge, round2
from app.utils.logo import load_logo_bytes
from app.utils.signature import load_signature_bytes
from app.utils.pagination import Page, paged_response
from app.utils.payments import check_not_overpaid, edit_payment, reverse_payment
from app.utils.pdf import build_purchase_pdf, build_receipt_pdf
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.audit import AuditRoute
from app.config import settings
from app.utils import attachment as attachment_util, image_gate, storage

router = APIRouter(route_class=AuditRoute, prefix="/purchases", tags=["purchases"], dependencies=[Depends(require_role("OWNER"))])


@router.get("")
async def list_purchases(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    payment_status: str | None = None,
    supplier_id: str | None = None,
    search: SearchTerm = None,  # purchase no, supplier's invoice no or supplier name
    status_filter: str | None = Query(default=None, alias="status", pattern="^(ACTIVE|CANCELLED)$"),
    page: Page = Depends(),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.*, s.name AS supplier_name, COUNT(*) OVER() AS _total FROM purchases p
               JOIN suppliers s ON s.id = p.supplier_id
               WHERE ($1::date IS NULL OR p.purchase_date >= $1)
                 AND ($2::date IS NULL OR p.purchase_date <= $2)
                 AND ($3::text IS NULL OR p.payment_status = $3)
                 AND ($4::uuid IS NULL OR p.supplier_id = $4)
                 AND ($5::text IS NULL OR p.purchase_no ILIKE '%'||$5||'%' ESCAPE '\\' OR p.supplier_invoice_no ILIKE '%'||$5||'%' ESCAPE '\\'
                      OR s.name ILIKE '%'||$5||'%' ESCAPE '\\')
                 AND ($6::text IS NULL OR p.status = $6)
               ORDER BY p.purchase_date DESC, p.created_at DESC, p.id LIMIT $7 OFFSET $8""",
            parse_date(from_), parse_date(to), payment_status, supplier_id, search, status_filter,
            page.limit, page.offset,
        )
    return paged_response(rows)


async def _refuse_if_debit_notes(conn, purchase_id: str, what: str) -> None:
    notes = await conn.fetch("SELECT debit_note_no FROM debit_notes WHERE purchase_id = $1 AND status = 'ACTIVE' ORDER BY debit_note_no", purchase_id)
    if notes:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"This purchase has debit note(s) ({', '.join(n['debit_note_no'] for n in notes)}). "
                                   f"Cancel them before you {what} the purchase.")


@router.get("/{purchase_id}")
async def get_purchase(purchase_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        purchase = await conn.fetchrow(
            """SELECT p.*, s.name AS supplier_name FROM purchases p JOIN suppliers s ON s.id = p.supplier_id
               WHERE p.id = $1""",
            purchase_id,
        )
        if purchase is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        items = await conn.fetch(
            """SELECT pi.*, pr.name AS product_name, pr.is_serialized,
                      (SELECT array_agg(w.barcode ORDER BY w.barcode) FROM part_warranties w
                        WHERE w.purchase_item_id = pi.id AND w.status = 'IN_STOCK' AND w.barcode IS NOT NULL) AS in_stock_barcodes
               FROM purchase_items pi JOIN products pr ON pr.id = pi.product_id WHERE pi.purchase_id = $1""",
            purchase_id,
        )
        debit_notes = await conn.fetch(
            """SELECT id, debit_note_no, note_date, total_amount, refund_amount, reason, status
               FROM debit_notes WHERE purchase_id = $1 ORDER BY created_at""", purchase_id)
        returned = {str(r["purchase_item_id"]): r["qty"] for r in await conn.fetch(
            """SELECT ni.purchase_item_id, SUM(ni.quantity) AS qty FROM debit_note_items ni
               JOIN debit_notes n ON n.id = ni.debit_note_id
               WHERE n.purchase_id = $1 AND n.status = 'ACTIVE' GROUP BY ni.purchase_item_id""", purchase_id)}
    result = row_to_dict(purchase)
    result["items"] = rows_to_list(items)
    for row in result["items"]:
        row["returned_quantity"] = returned.get(str(row["id"]), Decimal("0.00"))
    result["debit_notes"] = rows_to_list(debit_notes)
    result["has_attachment"] = bool(purchase["attachment_key"])
    result.pop("attachment_key", None)
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_purchase(body: CreatePurchaseRequest, current_user=Depends(get_current_user)):
    if not body.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one item required")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            purchase_no = await next_doc_number(conn, "purchases", "purchase_no", "PUR")

            subtotal = Decimal("0.00")
            total_gst = Decimal("0.00")
            computed_items = []
            for item in body.items:
                line_subtotal = round2(item.quantity * item.unit_price)
                line_gst = round2(line_subtotal * item.gst_rate / 100)
                subtotal += line_subtotal
                total_gst += line_gst
                computed_items.append({**item.model_dump(), "line_subtotal": line_subtotal,
                                        "line_gst": line_gst, "line_total": round2(line_subtotal + line_gst)})
            total_amount = round2(subtotal + total_gst)
            if total_amount > MAX_DOCUMENT_TOTAL:
                raise AmountTooLarge("The document total is too large")

            purchase = await conn.fetchrow(
                """INSERT INTO purchases (purchase_no, supplier_id, purchase_date, supplier_invoice_no, subtotal, total_gst, total_amount, notes, created_by)
                   VALUES ($1,$2,COALESCE($3,CURRENT_DATE),$4,$5,$6,$7,$8,$9) RETURNING *""",
                purchase_no, body.supplier_id, body.purchase_date, body.supplier_invoice_no,
                subtotal, total_gst, total_amount, body.notes, current_user["id"],
            )

            for item in computed_items:
                purchase_item = await conn.fetchrow(
                    """INSERT INTO purchase_items (purchase_id, product_id, quantity, unit_price, gst_rate, gst_amount, line_total)
                       VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
                    purchase["id"], item["product_id"], item["quantity"], item["unit_price"],
                    item["gst_rate"], item["line_gst"], item["line_total"],
                )
                await conn.execute(
                    """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, created_by)
                       VALUES ($1,$2,'PURCHASE',$3,$4)""",
                    item["product_id"], item["quantity"], purchase["id"], current_user["id"],
                )
                await conn.execute("UPDATE products SET purchase_price = $1 WHERE id = $2",
                                    item["unit_price"], item["product_id"])

                barcodes = item.get("barcodes")
                if barcodes:
                    if item["quantity"] != item["quantity"].to_integral_value() or len(barcodes) != int(item["quantity"]):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Line for product {item['product_id']} has quantity {item['quantity']} "
                                   f"but {len(barcodes)} barcode(s) were given — they must match 1:1",
                        )
                    months = item.get("supplier_warranty_months")
                    valid_until = add_months(purchase["purchase_date"], months) if months else None
                    for barcode in barcodes:
                        try:
                            await conn.execute(
                                """INSERT INTO part_warranties
                                       (product_id, barcode, purchase_id, purchase_item_id, purchase_date,
                                        supplier_warranty_months, supplier_warranty_valid_until, status, created_by)
                                   VALUES ($1,$2,$3,$4,$5,$6,$7,'IN_STOCK',$8)""",
                                item["product_id"], barcode, purchase["id"], purchase_item["id"],
                                purchase["purchase_date"], months, valid_until, current_user["id"],
                            )
                        except asyncpg.UniqueViolationError:
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=f"Barcode '{barcode}' is already tracked on another unit",
                            )

    result = row_to_dict(purchase)
    result["items"] = computed_items
    return result


@router.get("/{purchase_id}/payments")
async def list_purchase_payments(purchase_id: str, current_user=Depends(get_current_user)):
    """Payment history (newest first) for one purchase. The platform admin's name is hidden from
    everyone except the platform admin itself, same as everywhere else it could otherwise leak."""
    pool = get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM purchases WHERE id = $1", purchase_id)
        if not exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        rows = await conn.fetch(
            """SELECT p.id, p.amount, p.payment_mode, p.payment_date, p.reference_no, p.notes, p.created_at,
                      p.reversed_at, p.reversal_reason, p.reverses_payment_id, p.credit_note_id, p.debit_note_id,
                      CASE WHEN u.is_platform_admin AND u.id <> $2::uuid THEN NULL ELSE u.name END AS created_by_name
               FROM payments p LEFT JOIN users u ON u.id = p.created_by
               WHERE p.purchase_id = $1 ORDER BY p.payment_date DESC, p.created_at DESC, p.id""",
            purchase_id, current_user["id"],
        )
    return rows_to_list(rows)


@router.post("/{purchase_id}/payments")
async def add_purchase_payment(purchase_id: str, body: PaymentRequest, current_user=Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT id, status, total_amount, paid_amount FROM purchases WHERE id = $1 FOR UPDATE", purchase_id
            )
            if locked is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if locked["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This purchase is cancelled — payments can't be added")
            check_not_overpaid(body.amount, locked["total_amount"], locked["paid_amount"], "purchase")
            await conn.execute(
                """INSERT INTO payments (purchase_id, amount, payment_mode, payment_date, reference_no, notes, created_by)
                   VALUES ($1,$2,$3,COALESCE($4,CURRENT_DATE),$5,$6,$7)""",
                purchase_id, body.amount, body.payment_mode, body.payment_date,
                body.reference_no, body.notes, current_user["id"],
            )
            row = await conn.fetchrow(
                """UPDATE purchases SET paid_amount = paid_amount + $1,
                          payment_status = CASE WHEN paid_amount + $1 >= total_amount THEN 'PAID'
                                                 WHEN paid_amount + $1 > 0 THEN 'PARTIAL'
                                                 ELSE 'UNPAID' END
                   WHERE id = $2 RETURNING *""",
                body.amount, purchase_id,
            )
    return row_to_dict(row)


@router.put("/{purchase_id}/payments/{payment_id}")
async def update_purchase_payment(purchase_id: str, payment_id: str, body: UpdatePaymentRequest):
    """Fix the mode / date / reference / notes of a payment made to the supplier. The amount can't be edited:
    reverse the payment (POST .../reverse) and record the right one."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await edit_payment(conn, "purchase", purchase_id, payment_id,
                                 {k: getattr(body, k) for k in body.model_fields_set})
    return row_to_dict(row)


@router.post("/{purchase_id}/payments/{payment_id}/reverse")
async def reverse_purchase_payment(purchase_id: str, payment_id: str, body: ReversePaymentRequest,
                                   owner=Depends(require_role("OWNER"))):
    """OWNER only. Undoes a wrongly entered payment without deleting it (a negative payment is booked against the
    original, which is marked reversed with the reason). Refused (409) on a cancelled purchase and on a payment that
    was already reversed. Returns the updated purchase plus `reversal_id`."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await reverse_payment(conn, "purchase", purchase_id, payment_id, body.reason.strip(), owner["id"])
    purchase = row_to_dict(result["document"])
    purchase["reversal_id"] = result["reversal_id"]
    return purchase


@router.get("/{purchase_id}/payments/{payment_id}/voucher")
async def get_payment_voucher(purchase_id: str, payment_id: str):
    """PDF payment voucher for one payment made to a supplier (not for a refund or a reversed payment)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("SELECT * FROM payments WHERE id = $1 AND purchase_id = $2", payment_id, purchase_id)
        if payment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
        if payment["amount"] <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A voucher can only be made for a payment made")
        if payment["reversed_at"] is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This payment was reversed, so it has no voucher")
        purchase = await conn.fetchrow(
            """SELECT p.*, s.name AS supplier_name, s.gstin AS supplier_gstin
               FROM purchases p JOIN suppliers s ON s.id = p.supplier_id WHERE p.id = $1""",
            purchase_id,
        )
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    pdf_bytes = await asyncio.to_thread(build_receipt_pdf,
        store_data, "purchase", row_to_dict(purchase), row_to_dict(payment),
        {"name": purchase["supplier_name"], "gstin": purchase["supplier_gstin"]}, logo, signature)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="voucher-{str(payment["id"])[:8]}.pdf"'},
    )


@router.get("/{purchase_id}/pdf")
async def get_purchase_pdf(purchase_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        purchase = await conn.fetchrow(
            """SELECT p.*, s.name AS supplier_name, s.address AS supplier_address, s.gstin AS supplier_gstin,
                      s.phone AS supplier_phone, s.state AS supplier_state, s.state_code AS supplier_state_code
               FROM purchases p JOIN suppliers s ON s.id = p.supplier_id WHERE p.id = $1""",
            purchase_id,
        )
        if purchase is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        items = await conn.fetch(
            """SELECT pi.*, pr.name AS product_name, pr.hsn_code AS product_hsn_code,
                      (SELECT array_agg(w.barcode ORDER BY w.barcode) FROM part_warranties w
                        WHERE w.purchase_item_id = pi.id AND w.barcode IS NOT NULL) AS barcodes
               FROM purchase_items pi
               JOIN products pr ON pr.id = pi.product_id WHERE pi.purchase_id = $1 ORDER BY pi.id""",
            purchase_id,
        )
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")

    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    # An internal purchase record is not signed on the shop's behalf, so no signature is loaded for it.
    pdf_bytes = await asyncio.to_thread(build_purchase_pdf, store_data, row_to_dict(purchase), rows_to_list(items), logo)
    safe_name = purchase["purchase_no"].replace("/", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}.pdf"'},
    )


@router.put("/{purchase_id}")
async def update_purchase(purchase_id: str, body: UpdatePurchaseRequest, owner=Depends(require_role("OWNER"))):
    """OWNER only. Fix the header of a purchase: supplier, date, the supplier's invoice number, notes.
    Only the fields you send change (send null to clear the invoice number / notes). Changing the
    date moves the supplier-warranty clock of its units that still use the standard months-from-purchase
    date. The lines can't be edited: cancel the purchase and enter it again."""
    sent = body.model_fields_set
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            purchase = await conn.fetchrow("SELECT * FROM purchases WHERE id = $1 FOR UPDATE", purchase_id)
            if purchase is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if purchase["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A cancelled purchase can't be edited")
            await _refuse_if_debit_notes(conn, purchase_id, "edit")

            supplier_id = body.supplier_id if body.supplier_id is not None else str(purchase["supplier_id"])
            if supplier_id != str(purchase["supplier_id"]):
                if purchase["paid_amount"] > 0:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                        detail="Payments were already recorded against this supplier, so it can't be changed")
                if not await conn.fetchval("SELECT 1 FROM suppliers WHERE id = $1::uuid", supplier_id):
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

            new_date = body.purchase_date if body.purchase_date is not None else purchase["purchase_date"]
            invoice_no = body.supplier_invoice_no if "supplier_invoice_no" in sent else purchase["supplier_invoice_no"]
            if isinstance(invoice_no, str):
                invoice_no = invoice_no.strip() or None
            notes = body.notes if "notes" in sent else purchase["notes"]

            row = await conn.fetchrow(
                """UPDATE purchases SET supplier_id = $2::uuid, purchase_date = $3, supplier_invoice_no = $4, notes = $5
                   WHERE id = $1 RETURNING *""",
                purchase_id, supplier_id, new_date, invoice_no, notes,
            )

            old_date = purchase["purchase_date"]
            if new_date != old_date:
                units = await conn.fetch(
                    "SELECT id, supplier_warranty_months, supplier_warranty_valid_until FROM part_warranties WHERE purchase_id = $1",
                    purchase_id,
                )
                for u in units:
                    months = u["supplier_warranty_months"]
                    until = u["supplier_warranty_valid_until"]
                    # Only re-date units whose end date is still the plain "months after purchase" value;
                    # one the owner set by hand is left alone.
                    if months and until == add_months(old_date, months):
                        until = add_months(new_date, months)
                    await conn.execute(
                        "UPDATE part_warranties SET purchase_date = $2, supplier_warranty_valid_until = $3, updated_at = now() WHERE id = $1",
                        u["id"], new_date, until,
                    )
            supplier_name = await conn.fetchval("SELECT name FROM suppliers WHERE id = $1", row["supplier_id"])
    result = row_to_dict(row)
    result["supplier_name"] = supplier_name
    return result


@router.post("/{purchase_id}/cancel")
async def cancel_purchase(purchase_id: str, body: CancelDocumentRequest, owner=Depends(require_role("OWNER"))):
    """OWNER only. Cancels a purchase but keeps it on file. The stock it added is taken back out and its
    tracked units are removed (so the barcodes can be entered again), and it drops out of supplier
    balances, reports and the GST input credit.

    Refused (409) when any unit from it has left stock (sold, claimed, returned, scrapped), when it's
    on a service ticket, when there isn't enough stock left to take back, and while money is recorded
    against it unless `refund_payments` is true (then a negative payment books the refund)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            purchase = await conn.fetchrow("SELECT * FROM purchases WHERE id = $1 FOR UPDATE", purchase_id)
            if purchase is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if purchase["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This purchase is already cancelled")
            await _refuse_if_debit_notes(conn, purchase_id, "cancel")

            paid = purchase["paid_amount"]
            if paid > 0 and not body.refund_payments:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Rs. {paid:.2f} has been paid on this purchase. Get it refunded by the supplier, "
                           f"then cancel again and confirm the refund.",
                )

            units = await conn.fetch("SELECT id, barcode, status FROM part_warranties WHERE purchase_id = $1 FOR UPDATE", purchase_id)
            gone = [u for u in units if u["status"] != "IN_STOCK"]
            if gone:
                names = ", ".join(sorted(u["barcode"] or str(u["id"])[:8] for u in gone))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"These units have already left stock (sold, claimed, returned or scrapped): {names}",
                )
            if units and await conn.fetchval("SELECT 1 FROM service_tickets WHERE warranty_id = ANY($1::uuid[]) LIMIT 1",
                                             [u["id"] for u in units]):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail="A service ticket refers to a unit from this purchase")

            lines = await conn.fetch(
                """SELECT product_id, SUM(quantity) AS quantity FROM stock_ledger
                   WHERE reference_type = 'PURCHASE' AND reference_id = $1 GROUP BY product_id""",
                purchase_id,
            )
            if lines:
                stock = {
                    r["id"]: r for r in await conn.fetch(
                        "SELECT id, name, current_stock FROM products WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE",
                        [ln["product_id"] for ln in lines],
                    )
                }
                for ln in lines:
                    have = float(stock[ln["product_id"]]["current_stock"])
                    if have < float(ln["quantity"]):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Not enough {stock[ln['product_id']]['name']} in stock to take this purchase back "
                                   f"(have {have:g}, need {float(ln['quantity']):g}) — some of it has been used or sold",
                        )

            if paid > 0:
                last_mode = await conn.fetchval(
                    "SELECT payment_mode FROM payments WHERE purchase_id = $1 ORDER BY created_at DESC LIMIT 1", purchase_id
                )
                await conn.execute(
                    """INSERT INTO payments (purchase_id, amount, payment_mode, payment_date, notes, created_by)
                       VALUES ($1, $2, $3, CURRENT_DATE, $4, $5)""",
                    purchase_id, -paid, last_mode or "CASH", f"Refund on cancellation of {purchase['purchase_no']}", owner["id"],
                )

            await conn.execute(
                """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                   SELECT product_id, -quantity, 'RETURN_OUT', reference_id, $2::text, $3::uuid
                   FROM stock_ledger WHERE reference_type = 'PURCHASE' AND reference_id = $1""",
                purchase_id, f"Purchase {purchase['purchase_no']} cancelled", owner["id"],
            )
            if units:
                await conn.execute("DELETE FROM part_warranties WHERE purchase_id = $1", purchase_id)

            # The purchase price it set goes back to the latest earlier purchase's price (only if nobody changed it since).
            for item in await conn.fetch("SELECT product_id, unit_price FROM purchase_items WHERE purchase_id = $1", purchase_id):
                await conn.execute(
                    """UPDATE products p SET purchase_price = COALESCE(
                              (SELECT pi.unit_price FROM purchase_items pi JOIN purchases pu ON pu.id = pi.purchase_id
                               WHERE pi.product_id = p.id AND pu.status = 'ACTIVE' AND pu.id <> $1
                               ORDER BY pu.purchase_date DESC, pu.created_at DESC LIMIT 1), p.purchase_price)
                       WHERE p.id = $2 AND p.purchase_price = $3""",
                    purchase_id, item["product_id"], item["unit_price"],
                )

            row = await conn.fetchrow(
                """UPDATE purchases SET status = 'CANCELLED', cancelled_at = now(), cancel_reason = $2, cancelled_by = $3,
                          paid_amount = 0,
                          payment_status = CASE WHEN paid_amount > 0 THEN 'UNPAID' ELSE payment_status END
                   WHERE id = $1 RETURNING *""",
                purchase_id, body.reason.strip(), owner["id"],
            )
    return row_to_dict(row)


# ---------- the scanned / photographed supplier invoice kept with the purchase ----------
@router.post("/{purchase_id}/attachment")
async def upload_purchase_attachment(purchase_id: str, file: UploadFile = File(...), current_user=Depends(get_current_user)):
    """Multipart form, field `file`: the supplier's invoice as a PDF or a PNG / JPEG / WebP photo (up to MAX_ATTACHMENT_MB).
    Photos are straightened and shrunk; replaces any earlier attachment. Returns `{"has_attachment": true, "attachment_type", "attachment_name"}`."""
    max_bytes = settings.max_attachment_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty")
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"The file must be under {settings.max_attachment_mb} MB")
    try:
        stored, content_type, ext = await image_gate.run_limited(attachment_util.normalise, data)
    except attachment_util.AttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    key = attachment_util.new_key(ext)
    name = (file.filename or f"supplier-invoice.{ext}").replace("\\", "/").split("/")[-1][:200]
    pool = get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM purchases WHERE id = $1", purchase_id)
        if not exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    await asyncio.to_thread(storage.put_bytes, key, stored, content_type)
    old_key = None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                old_key = await conn.fetchval("SELECT attachment_key FROM purchases WHERE id = $1 FOR UPDATE", purchase_id)
                await conn.execute(
                    "UPDATE purchases SET attachment_key = $2, attachment_type = $3, attachment_name = $4 WHERE id = $1",
                    purchase_id, key, content_type, name)
    except Exception:
        await asyncio.to_thread(storage.delete_keys, [key])
        raise
    if old_key:
        await asyncio.to_thread(storage.delete_keys, [old_key])
    return {"has_attachment": True, "attachment_type": content_type, "attachment_name": name}


@router.get("/{purchase_id}/attachment")
async def get_purchase_attachment(purchase_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT attachment_key, attachment_type, attachment_name FROM purchases WHERE id = $1", purchase_id)
    if row is None or not row["attachment_key"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No file is attached to this purchase")
    try:
        data = await asyncio.to_thread(storage.get_bytes, row["attachment_key"])
    except storage.ObjectMissing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The attached file is missing on the server")
    filename = (row["attachment_name"] or "supplier-invoice").replace('"', "")
    return Response(content=data, media_type=row["attachment_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "private, no-cache"})


@router.delete("/{purchase_id}/attachment", status_code=status.HTTP_204_NO_CONTENT)
async def delete_purchase_attachment(purchase_id: str, owner=Depends(require_role("OWNER"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            key = await conn.fetchval("SELECT attachment_key FROM purchases WHERE id = $1 FOR UPDATE", purchase_id)
            await conn.execute("UPDATE purchases SET attachment_key = NULL, attachment_type = NULL, attachment_name = NULL WHERE id = $1", purchase_id)
    if key:
        await asyncio.to_thread(storage.delete_keys, [key])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
