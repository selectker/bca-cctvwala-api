import asyncio
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.billing import CreateQuotationRequest, UpdateQuotationStatusRequest
from app.security import get_current_user, require_role
from app.utils.doc_number import doc_prefix, next_doc_number
from app.utils.gst import compute_invoice_totals, compute_lines_with_invoice_discount, get_store_state_code, round2
from app.utils.pagination import Page, paged_response
from app.utils.logo import load_logo_bytes
from app.utils.signature import load_signature_bytes
from app.utils.pdf import build_quotation_pdf
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.audit import AuditRoute

router = APIRouter(route_class=AuditRoute, prefix="/quotations", tags=["quotations"], dependencies=[Depends(require_role("OWNER"))])


@router.get("")
async def list_quotations(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: str | None = None,
    search: SearchTerm = None,
    page: Page = Depends(),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT q.*, c.name AS customer_name, inv.invoice_no AS converted_invoice_no,
                      COUNT(*) OVER() AS _total
               FROM quotations q
               JOIN customers c ON c.id = q.customer_id
               LEFT JOIN invoices inv ON inv.id = q.converted_invoice_id
               WHERE ($1::text IS NULL OR q.status = $1)
                 AND ($2::uuid IS NULL OR q.customer_id = $2)
                 AND ($3::text IS NULL OR q.quotation_no ILIKE '%'||$3||'%' ESCAPE '\\' OR c.name ILIKE '%'||$3||'%' ESCAPE '\\')
               ORDER BY q.quotation_date DESC, q.created_at DESC, q.id LIMIT $4 OFFSET $5""",
            status_filter, customer_id, search, page.limit, page.offset,
        )
    return paged_response(rows)


@router.get("/{quotation_id}")
async def get_quotation(quotation_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        quotation = await conn.fetchrow(
            """SELECT q.*, c.name AS customer_name, c.state_code, inv.invoice_no AS converted_invoice_no
               FROM quotations q JOIN customers c ON c.id = q.customer_id
               LEFT JOIN invoices inv ON inv.id = q.converted_invoice_id WHERE q.id = $1""",
            quotation_id,
        )
        if quotation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        items = await conn.fetch(
            """SELECT qi.*, p.name AS product_name, p.is_serialized FROM quotation_items qi
               LEFT JOIN products p ON p.id = qi.product_id WHERE qi.quotation_id = $1 ORDER BY qi.id""",
            quotation_id,
        )
    result = row_to_dict(quotation)
    result["items"] = rows_to_list(items)
    return result


async def _price_items(conn, customer_id: str, items, discount_percent=0, discount_amount=0):
    """GST for every line, worked out from the customer's state. Returns (computed, totals, total_gst)."""
    customer = await conn.fetchrow("SELECT state_code FROM customers WHERE id = $1", customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    buyer_state_code = customer["state_code"]
    store_state_code = await get_store_state_code(conn)
    try:
        lines, document_discount = compute_lines_with_invoice_discount(
            [(i.quantity, i.unit_price, i.gst_rate, i.discount_percent) for i in items],
            buyer_state_code, store_state_code, discount_percent, discount_amount)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    computed = list(zip(items, lines))
    totals = compute_invoice_totals(lines)
    return computed, totals, totals.total_cgst + totals.total_sgst + totals.total_igst, document_discount


async def _insert_items(conn, quotation_id, computed) -> list[dict]:
    result_items = []
    for item, gst in computed:
        gst_amount = gst.cgst + gst.sgst + gst.igst
        await conn.execute(
            """INSERT INTO quotation_items (quotation_id, product_id, description, quantity, unit_price, gst_rate,
                                            gst_amount, line_total, customer_warranty_months,
                                            discount_percent, discount_amount)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            quotation_id, item.product_id, item.description, item.quantity,
            item.unit_price, item.gst_rate, gst_amount, gst.line_total, item.customer_warranty_months,
            item.discount_percent, gst.discount_amount,
        )
        result_items.append({**item.model_dump(), "gst_amount": gst_amount, "line_total": gst.line_total,
                             "discount_amount": gst.discount_amount})
    return result_items


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_quotation(body: CreateQuotationRequest, current_user=Depends(get_current_user)):
    if not body.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one item required")

    pool = get_pool()
    async with pool.acquire() as conn:
        computed, totals, total_gst, document_discount = await _price_items(
            conn, body.customer_id, body.items, body.invoice_discount_percent, body.invoice_discount_amount)

        async with conn.transaction():
            quotation_no = await next_doc_number(
                conn, "quotations", "quotation_no", await doc_prefix(conn, "quotation_prefix", "QUO")
            )
            quotation = await conn.fetchrow(
                """INSERT INTO quotations (quotation_no, customer_id, valid_till, subtotal, total_gst, total_amount, notes, created_by,
                                           total_discount, invoice_discount_percent, invoice_discount_amount)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
                quotation_no, body.customer_id, body.valid_till, totals.subtotal,
                total_gst, totals.total_amount, body.notes, current_user["id"], totals.total_discount,
                body.invoice_discount_percent if not body.invoice_discount_amount else 0, document_discount,
            )
            result_items = await _insert_items(conn, quotation["id"], computed)

    result = row_to_dict(quotation)
    result["items"] = result_items
    return result


@router.put("/{quotation_id}")
async def update_quotation(quotation_id: str, body: CreateQuotationRequest):
    """Revise a quotation that is still PENDING: customer, validity, notes and the lines are replaced and
    the totals re-worked. The number and date stay. Accepted / rejected / converted ones are frozen."""
    if not body.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one item required")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT status FROM quotations WHERE id = $1 FOR UPDATE", quotation_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if existing["status"] != "PENDING":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Only a pending quotation can be edited (this one is {existing['status'].lower()})",
                )
            computed, totals, total_gst, document_discount = await _price_items(
                conn, body.customer_id, body.items, body.invoice_discount_percent, body.invoice_discount_amount)
            quotation = await conn.fetchrow(
                """UPDATE quotations SET customer_id = $2, valid_till = $3, subtotal = $4, total_gst = $5,
                          total_amount = $6, notes = $7, total_discount = $8,
                          invoice_discount_percent = $9, invoice_discount_amount = $10
                   WHERE id = $1 RETURNING *""",
                quotation_id, body.customer_id, body.valid_till, totals.subtotal, total_gst,
                totals.total_amount, body.notes, totals.total_discount,
                body.invoice_discount_percent if not body.invoice_discount_amount else 0, document_discount,
            )
            await conn.execute("DELETE FROM quotation_items WHERE quotation_id = $1", quotation_id)
            result_items = await _insert_items(conn, quotation_id, computed)

    result = row_to_dict(quotation)
    result["items"] = result_items
    return result


@router.get("/{quotation_id}/pdf")
async def get_quotation_pdf(quotation_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        quotation = await conn.fetchrow(
            """SELECT q.*, c.name AS customer_name, c.address AS customer_address, c.gstin AS customer_gstin,
                      c.phone AS customer_phone, c.state AS customer_state, c.state_code AS customer_state_code
               FROM quotations q JOIN customers c ON c.id = q.customer_id WHERE q.id = $1""",
            quotation_id,
        )
        if quotation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        items = await conn.fetch(
            """SELECT qi.*, p.name AS product_name, p.hsn_code AS product_hsn_code FROM quotation_items qi
               LEFT JOIN products p ON p.id = qi.product_id WHERE qi.quotation_id = $1 ORDER BY qi.id""",
            quotation_id,
        )
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
        store_state = await get_store_state_code(conn)

    data = row_to_dict(quotation)
    gst = data["total_gst"] or Decimal("0")
    interstate = bool(data.get("customer_state_code") and data["customer_state_code"] != store_state)
    half = round2(gst / 2)
    data["total_igst"] = gst if interstate else Decimal("0.00")
    data["total_cgst"] = Decimal("0.00") if interstate else half
    data["total_sgst"] = Decimal("0.00") if interstate else round2(gst - half)

    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    pdf_bytes = await asyncio.to_thread(build_quotation_pdf, store_data, data, rows_to_list(items), logo, signature)
    safe_name = quotation["quotation_no"].replace("/", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}.pdf"'},
    )


@router.put("/{quotation_id}/status")
async def update_quotation_status(quotation_id: str, body: UpdateQuotationStatusRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT status FROM quotations WHERE id = $1", quotation_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if existing["status"] == "CONVERTED":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This quotation was already converted to an invoice")
        row = await conn.fetchrow(
            "UPDATE quotations SET status = $1 WHERE id = $2 RETURNING *", body.status, quotation_id
        )
    return row_to_dict(row)
