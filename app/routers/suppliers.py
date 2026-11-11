import asyncio
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.parties import CreateSupplierRequest, UpdateSupplierRequest
from app.security import get_current_user, require_role
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list
from app.utils import ledger
from app.utils.audit import AuditRoute

router = APIRouter(route_class=AuditRoute, prefix="/suppliers", tags=["suppliers"], dependencies=[Depends(require_role("OWNER"))])


_WITH_PAYABLE = """
    SELECT s.*, COALESCE(s.opening_balance, 0) + COALESCE(d.due, 0) AS payable
    FROM suppliers s
    LEFT JOIN LATERAL (
        SELECT SUM(p.total_amount - p.paid_amount) AS due FROM purchases p WHERE p.supplier_id = s.id AND p.status = 'ACTIVE'
    ) d ON TRUE
"""


@router.get("")
async def list_suppliers(search: SearchTerm = None, page: Page = Depends()):
    """Active suppliers with the current `payable` amount (opening balance + unpaid purchases)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT t.*, COUNT(*) OVER() AS _total FROM ({_WITH_PAYABLE}) t
                WHERE t.is_active = TRUE AND ($1::text IS NULL OR t.name ILIKE '%'||$1||'%' ESCAPE '\\' OR t.phone ILIKE '%'||$1||'%' ESCAPE '\\')
                ORDER BY t.name, t.id LIMIT $2 OFFSET $3""",
            search, page.limit, page.offset,
        )
    return paged_response(rows)


@router.get("/{supplier_id}")
async def get_supplier(supplier_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT t.* FROM ({_WITH_PAYABLE}) t WHERE t.id = $1", supplier_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row_to_dict(row)


@router.get("/{supplier_id}/balance")
async def get_supplier_balance(supplier_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(s.opening_balance, 0) AS opening_balance,
                      COALESCE(SUM(p.total_amount - p.paid_amount), 0) AS purchase_due
               FROM suppliers s
               LEFT JOIN purchases p ON p.supplier_id = s.id AND p.status = 'ACTIVE'
               WHERE s.id = $1
               GROUP BY s.opening_balance""",
            supplier_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {
        "payable": float(row["opening_balance"] + row["purchase_due"]),
        "opening_balance": float(row["opening_balance"]),
        "purchase_due": float(row["purchase_due"]),
    }


@router.get("/{supplier_id}/ledger")
async def get_supplier_ledger(supplier_id: str, from_: date | None = Query(default=None, alias="from"), to: date | None = None):
    """Every transaction with this supplier in date order, with a running balance (see app/utils/ledger.py for the rules).
    `from` / `to` (YYYY-MM-DD) narrow the period; the opening balance is then what stood before `from`."""
    pool = get_pool()
    async with pool.acquire() as conn:
        data = await ledger.supplier_ledger(conn, supplier_id, from_, to)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return jsonable_encoder(data)


@router.get("/{supplier_id}/ledger/pdf")
async def get_supplier_ledger_pdf(supplier_id: str, from_: date | None = Query(default=None, alias="from"), to: date | None = None):
    pool = get_pool()
    async with pool.acquire() as conn:
        data = await ledger.supplier_ledger(conn, supplier_id, from_, to)
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    pdf = await asyncio.to_thread(ledger.ledger_pdf, row_to_dict(store) or {}, data, from_, to)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="supplier-ledger.pdf"'})


@router.get("/{supplier_id}/history")
async def get_supplier_history(supplier_id: str, page: Page = Depends()):
    """Everything bought from this supplier: purchases (newest first, paged — X-Total-Count is the
    supplier's purchase count), each with its line items and — for serialized items — the exact
    unit's warranty and, if it's been sold, which customer and invoice it went out on. This is
    the deep purchase<->sale<->warranty link."""
    pool = get_pool()
    async with pool.acquire() as conn:
        supplier = await conn.fetchrow("SELECT * FROM suppliers WHERE id = $1", supplier_id)
        if supplier is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        purchases = await conn.fetch(
            """SELECT pu.*, COUNT(*) OVER() AS _total FROM purchases pu WHERE pu.supplier_id = $1
               ORDER BY pu.purchase_date DESC, pu.created_at DESC, pu.id LIMIT $2 OFFSET $3""",
            supplier_id, page.limit, page.offset,
        )
        total = int(purchases[0]["_total"]) if purchases else 0
        if purchases:
            items = await conn.fetch(
                """SELECT pi.*, p.name AS product_name, p.sku AS product_sku, p.is_serialized,
                          w.id AS warranty_id, w.barcode, w.status AS warranty_status,
                          w.supplier_warranty_valid_until, w.customer_warranty_valid_until,
                          w.invoice_id, inv.invoice_no, c.name AS customer_name
                   FROM purchase_items pi
                   LEFT JOIN products p ON p.id = pi.product_id
                   LEFT JOIN part_warranties w ON w.purchase_item_id = pi.id
                   LEFT JOIN invoices inv ON inv.id = w.invoice_id
                   LEFT JOIN customers c ON c.id = inv.customer_id
                   WHERE pi.purchase_id = ANY($1::uuid[])
                   ORDER BY pi.id""",
                [pu["id"] for pu in purchases],
            )
        else:
            items = []

    items_by_purchase: dict[str, list[dict]] = {}
    for row in rows_to_list(items):
        items_by_purchase.setdefault(str(row["purchase_id"]), []).append(row)

    result = row_to_dict(supplier)
    result["purchases"] = [
        {**{k: v for k, v in dict(pu).items() if k != "_total"}, "items": items_by_purchase.get(str(pu["id"]), [])}
        for pu in purchases
    ]
    return JSONResponse(content=jsonable_encoder(result), headers={"X-Total-Count": str(total)})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_supplier(body: CreateSupplierRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO suppliers (name, gstin, phone, email, address, state, state_code, opening_balance)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
            body.name, body.gstin, body.phone, body.email, body.address,
            body.state, body.state_code, body.opening_balance,
        )
    return row_to_dict(row)


@router.put("/{supplier_id}")
async def update_supplier(supplier_id: str, body: UpdateSupplierRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE suppliers SET name=$1, gstin=$2, phone=$3, email=$4, address=$5, state=$6, state_code=$7
               WHERE id=$8 RETURNING *""",
            body.name, body.gstin, body.phone, body.email, body.address,
            body.state, body.state_code, supplier_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row_to_dict(row)


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_supplier(supplier_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE suppliers SET is_active = FALSE WHERE id = $1", supplier_id)
    return None
