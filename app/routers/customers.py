import asyncio
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.parties import CreateCustomerRequest, UpdateCustomerRequest
from app.security import get_current_user, require_role
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list
from app.utils import ledger
from app.utils.audit import AuditRoute
from app.utils.ticket_refs import tickets_for_customer
from app.utils.walk_in import get_or_create_walk_in

router = APIRouter(route_class=AuditRoute, prefix="/customers", tags=["customers"], dependencies=[Depends(require_role("OWNER"))])


_WITH_OUTSTANDING = """
    SELECT c.*, COALESCE(c.opening_balance, 0) + COALESCE(d.due, 0) AS outstanding
    FROM customers c
    LEFT JOIN LATERAL (
        SELECT SUM(i.total_amount - i.paid_amount) AS due FROM invoices i WHERE i.customer_id = c.id AND i.status = 'ACTIVE'
    ) d ON TRUE
"""


@router.get("/walk-in")
async def get_walk_in_customer():
    """The shop's one protected "Walk-in Customer" (made on first use). Counter sales are billed to it; it is left out of
    GET /customers so the app can pin it at the top of the picker instead."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return await get_or_create_walk_in(conn)


async def _refuse_if_walk_in(conn, customer_id: str) -> None:
    if await conn.fetchval("SELECT is_walk_in FROM customers WHERE id = $1", customer_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The walk-in customer is built in and can't be changed or deleted")


@router.get("")
async def list_customers(search: SearchTerm = None, page: Page = Depends()):
    """Active customers with their current `outstanding` amount (opening balance + unpaid invoices). The built-in walk-in
    customer is not listed here; get it from GET /customers/walk-in."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT t.*, COUNT(*) OVER() AS _total FROM ({_WITH_OUTSTANDING}) t
                WHERE t.is_active = TRUE AND t.is_walk_in = FALSE AND ($1::text IS NULL OR t.name ILIKE '%'||$1||'%' ESCAPE '\\' OR t.phone ILIKE '%'||$1||'%' ESCAPE '\\')
                ORDER BY t.name, t.id LIMIT $2 OFFSET $3""",
            search, page.limit, page.offset,
        )
    return paged_response(rows)


@router.get("/{customer_id}")
async def get_customer(customer_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT t.* FROM ({_WITH_OUTSTANDING}) t WHERE t.id = $1", customer_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row_to_dict(row)


@router.get("/{customer_id}/balance")
async def get_customer_balance(customer_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(c.opening_balance, 0) AS opening_balance,
                      COALESCE(SUM(i.total_amount - i.paid_amount), 0) AS invoice_due
               FROM customers c
               LEFT JOIN invoices i ON i.customer_id = c.id AND i.status = 'ACTIVE'
               WHERE c.id = $1
               GROUP BY c.opening_balance""",
            customer_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {
        "outstanding": float(row["opening_balance"] + row["invoice_due"]),
        "opening_balance": float(row["opening_balance"]),
        "invoice_due": float(row["invoice_due"]),
    }


@router.get("/{customer_id}/ledger")
async def get_customer_ledger(customer_id: str, from_: date | None = Query(default=None, alias="from"), to: date | None = None):
    """Every transaction with this customer in date order, with a running balance (see app/utils/ledger.py for the rules).
    `from` / `to` (YYYY-MM-DD) narrow the period; the opening balance is then what stood before `from`."""
    pool = get_pool()
    async with pool.acquire() as conn:
        data = await ledger.customer_ledger(conn, customer_id, from_, to)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return jsonable_encoder(data)


@router.get("/{customer_id}/ledger/pdf")
async def get_customer_ledger_pdf(customer_id: str, from_: date | None = Query(default=None, alias="from"), to: date | None = None):
    pool = get_pool()
    async with pool.acquire() as conn:
        data = await ledger.customer_ledger(conn, customer_id, from_, to)
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    pdf = await asyncio.to_thread(ledger.ledger_pdf, row_to_dict(store) or {}, data, from_, to)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="customer-ledger.pdf"'})


@router.get("/{customer_id}/history")
async def get_customer_history(customer_id: str, page: Page = Depends()):
    """Everything this customer bought: invoices (newest first, paged — X-Total-Count is the
    customer's invoice count), each with its line items and — for serialized items — the exact
    unit's warranty, traced back to the purchase and supplier it originally came from. This is
    the deep purchase<->sale<->warranty link. Also `service_tickets` (the customer's newest 50 service requests) and
    `service_ticket_count` (all of them)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        customer = await conn.fetchrow("SELECT * FROM customers WHERE id = $1", customer_id)
        if customer is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        invoices = await conn.fetch(
            """SELECT i.*, COUNT(*) OVER() AS _total FROM invoices i WHERE i.customer_id = $1
               ORDER BY i.invoice_date DESC, i.created_at DESC, i.id LIMIT $2 OFFSET $3""",
            customer_id, page.limit, page.offset,
        )
        total = int(invoices[0]["_total"]) if invoices else 0
        if invoices:
            items = await conn.fetch(
                """SELECT ii.*, p.name AS product_name, p.sku AS product_sku, p.is_serialized,
                          w.id AS warranty_id, w.barcode, w.status AS warranty_status,
                          w.customer_warranty_valid_until, w.supplier_warranty_valid_until,
                          w.purchase_id, pu.purchase_no, s.name AS supplier_name
                   FROM invoice_items ii
                   LEFT JOIN products p ON p.id = ii.product_id
                   LEFT JOIN part_warranties w ON w.invoice_item_id = ii.id
                   LEFT JOIN purchases pu ON pu.id = w.purchase_id
                   LEFT JOIN suppliers s ON s.id = pu.supplier_id
                   WHERE ii.invoice_id = ANY($1::uuid[])
                   ORDER BY ii.id""",
                [inv["id"] for inv in invoices],
            )
        else:
            items = []
        tickets, ticket_count = await tickets_for_customer(conn, customer_id)

    items_by_invoice: dict[str, list[dict]] = {}
    for row in rows_to_list(items):
        items_by_invoice.setdefault(str(row["invoice_id"]), []).append(row)

    result = row_to_dict(customer)
    result["invoices"] = [
        {**{k: v for k, v in dict(inv).items() if k != "_total"}, "items": items_by_invoice.get(str(inv["id"]), [])}
        for inv in invoices
    ]
    # The customer's service requests (newest 50, whatever page of invoices was asked for), so one screen shows
    # both what they bought and what they asked us to fix.
    result["service_tickets"] = tickets
    result["service_ticket_count"] = ticket_count
    return JSONResponse(content=jsonable_encoder(result), headers={"X-Total-Count": str(total)})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_customer(body: CreateCustomerRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO customers (name, gstin, phone, email, address, state, state_code, customer_type, opening_balance, shipping_address,
                                      credit_limit, payment_terms_days)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *""",
            body.name, body.gstin, body.phone, body.email, body.address,
            body.state, body.state_code, body.customer_type, body.opening_balance,
            (body.shipping_address or "").strip() or None, body.credit_limit, body.payment_terms_days,
        )
    return row_to_dict(row)


@router.put("/{customer_id}")
async def update_customer(customer_id: str, body: UpdateCustomerRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        await _refuse_if_walk_in(conn, customer_id)
        row = await conn.fetchrow(
            """UPDATE customers SET name=$1, gstin=$2, phone=$3, email=$4, address=$5, state=$6, state_code=$7, customer_type=$8,
                      shipping_address = CASE WHEN $10 THEN $11 ELSE shipping_address END,
                      credit_limit = CASE WHEN $12 THEN $13 ELSE credit_limit END,
                      payment_terms_days = CASE WHEN $14 THEN $15 ELSE payment_terms_days END
               WHERE id=$9 RETURNING *""",
            body.name, body.gstin, body.phone, body.email, body.address,
            body.state, body.state_code, body.customer_type, customer_id,
            # An older app that doesn't send shipping_address leaves it alone; sending "" or null clears it.
            "shipping_address" in body.model_fields_set, (body.shipping_address or "").strip() or None,
            "credit_limit" in body.model_fields_set, body.credit_limit,
            "payment_terms_days" in body.model_fields_set, body.payment_terms_days,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row_to_dict(row)


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(customer_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        await _refuse_if_walk_in(conn, customer_id)
        await conn.execute("UPDATE customers SET is_active = FALSE WHERE id = $1", customer_id)
    return None
