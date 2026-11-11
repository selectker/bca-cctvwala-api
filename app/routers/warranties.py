import asyncio
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.warranty import (
    ClaimWarrantyRequest,
    CreateWarrantyRequest,
    ReplaceWarrantyRequest,
    SellWarrantyRequest,
    UpdateWarrantyRequest,
)
from app.security import get_current_user, require_role
from app.utils.dates import add_months
from app.utils.logo import load_logo_bytes
from app.utils.pdf import build_warranty_card_pdf
from app.utils.signature import load_signature_bytes
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.audit import AuditRoute
from app.utils.ticket_refs import tickets_for_warranty

router = APIRouter(route_class=AuditRoute, prefix="/warranties", tags=["warranties"], dependencies=[Depends(require_role("OWNER"))])

_SELECT = """
    SELECT w.*, p.name AS product_name, p.sku AS product_sku,
           s.name AS supplier_name, c.name AS customer_name,
           i.invoice_no AS invoice_no, pu.purchase_no AS purchase_no,
           rw.barcode AS replaces_barcode, rb.barcode AS replaced_by_barcode
    FROM part_warranties w
    JOIN products p ON p.id = w.product_id
    LEFT JOIN part_warranties rw ON rw.id = w.replaces_warranty_id
    LEFT JOIN part_warranties rb ON rb.id = w.replaced_by_warranty_id
    LEFT JOIN purchases pu ON pu.id = w.purchase_id
    LEFT JOIN suppliers s ON s.id = pu.supplier_id
    LEFT JOIN invoices i ON i.id = w.invoice_id
    LEFT JOIN customers c ON c.id = i.customer_id
"""


@router.get("")
async def list_warranties(
    search: SearchTerm = None,      # matches barcode or product name/SKU
    product_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: Page = Depends(),
):
    pool = get_pool()
    query = f"""
        SELECT t.*, COUNT(*) OVER() AS _total FROM ({_SELECT}) t
        WHERE ($1::text IS NULL OR t.barcode ILIKE '%'||$1||'%' ESCAPE '\\' OR t.product_name ILIKE '%'||$1||'%' ESCAPE '\\' OR t.product_sku ILIKE '%'||$1||'%' ESCAPE '\\')
          AND ($2::uuid IS NULL OR t.product_id = $2)
          AND ($3::text IS NULL OR t.status = $3)
        ORDER BY t.created_at DESC, t.id LIMIT $4 OFFSET $5
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, search, product_id, status_filter, page.limit, page.offset)
    return paged_response(rows)


@router.get("/expiring")
async def expiring_warranties(
    days: int = Query(default=30, ge=0, le=3650),
    scope: str = "all",
    include_expired: bool = True,
    page: Page = Depends(),
):
    """Warranties running out within `days`. scope: 'supplier', 'customer', or 'all' (default).
    include_expired=false returns only ones that haven't lapsed yet (today .. today+days) — that is what
    reminders/badges should use, otherwise every long-expired unit is counted forever. The total is in X-Total-Count."""
    pool = get_pool()
    lower = "" if include_expired else "{col} >= CURRENT_DATE AND "
    conditions = []
    if scope in ("supplier", "all"):
        col = "w.supplier_warranty_valid_until"
        conditions.append(f"({col} IS NOT NULL AND {lower.format(col=col)}{col} <= CURRENT_DATE + $1::int * INTERVAL '1 day')")
    if scope in ("customer", "all"):
        col = "w.customer_warranty_valid_until"
        conditions.append(f"({col} IS NOT NULL AND {lower.format(col=col)}{col} <= CURRENT_DATE + $1::int * INTERVAL '1 day')")
    if not conditions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="scope must be supplier, customer, or all")
    where_clause = " OR ".join(conditions)
    query = f"""
        SELECT t.*, COUNT(*) OVER() AS _total FROM (
            {_SELECT}
            WHERE w.status NOT IN ('RETURNED', 'SCRAPPED', 'REPLACED') AND ({where_clause})
        ) t
        ORDER BY LEAST(
            COALESCE(t.supplier_warranty_valid_until, 'infinity'::date),
            COALESCE(t.customer_warranty_valid_until, 'infinity'::date)
        ), t.id
        LIMIT $2 OFFSET $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, days, page.limit, page.offset)
    return paged_response(rows)


@router.get("/lookup/{barcode}")
async def lookup_by_barcode(barcode: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"{_SELECT} WHERE w.barcode = $1", barcode)
        tickets = await tickets_for_warranty(conn, row["id"]) if row is not None else []
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No part found with that barcode")
    return {**row_to_dict(row), "service_tickets": tickets}


@router.get("/{warranty_id}/pdf")
async def get_warranty_card_pdf(warranty_id: str):
    """Customer warranty card for a unit that has been sold (it needs the sale and the customer warranty dates)."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT x.*, cu.phone AS customer_phone, cu.address AS customer_address, cu.gstin AS customer_gstin,
                           iv.invoice_date AS invoice_date
                    FROM ({_SELECT} WHERE w.id = $1) x
                    LEFT JOIN invoices iv ON iv.id = x.invoice_id
                    LEFT JOIN customers cu ON cu.id = iv.customer_id""",
                warranty_id,
            )
            store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    except asyncpg.DataError:  # not a valid id
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if row["invoice_id"] is None or row["customer_warranty_valid_until"] is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A warranty card is only for a unit that has been sold with a customer warranty")
    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    pdf_bytes = await asyncio.to_thread(build_warranty_card_pdf, store_data, row_to_dict(row), logo, signature)
    safe_name = ("WARRANTY-" + (row["barcode"] or str(row["id"])[:8])).replace("/", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}.pdf"'},
    )


@router.get("/{warranty_id}")
async def get_warranty(warranty_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"{_SELECT} WHERE w.id = $1", warranty_id)
        tickets = await tickets_for_warranty(conn, row["id"]) if row is not None else []
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {**row_to_dict(row), "service_tickets": tickets}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_warranty(body: CreateWarrantyRequest, current_user=Depends(get_current_user)):
    pool = get_pool()
    purchase_date = body.purchase_date
    valid_until = body.supplier_warranty_valid_until
    if valid_until is None and body.supplier_warranty_months and purchase_date:
        valid_until = add_months(purchase_date, body.supplier_warranty_months)

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO part_warranties
                       (product_id, barcode, purchase_id, purchase_item_id, purchase_date,
                        supplier_warranty_months, supplier_warranty_valid_until, notes, created_by)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
                body.product_id, body.barcode, body.purchase_id, body.purchase_item_id, purchase_date,
                body.supplier_warranty_months, valid_until, body.notes, current_user["id"],
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This barcode is already tracked")
    return row_to_dict(row)


# Statuses that can be set by hand. SOLD / CLAIMED / REPLACED only ever come from the sell, claim and
# replace actions, because those also maintain the invoice link and the replacement trail.
_MANUAL_STATUSES = {"IN_STOCK", "RETURNED", "SCRAPPED"}


@router.put("/{warranty_id}")
async def update_warranty(warranty_id: str, body: UpdateWarrantyRequest, current_user=Depends(get_current_user)):
    """Fix a tracked unit: barcode, product, supplier / customer warranty, notes, and mark it
    RETURNED (to supplier) or SCRAPPED. Only fields you send are changed. Leaving / re-entering
    IN_STOCK also moves the product's stock ledger so the stock count stays right."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT * FROM part_warranties WHERE id = $1 FOR UPDATE", warranty_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

            barcode = body.barcode.strip() if body.barcode and body.barcode.strip() else existing["barcode"]
            product_id = body.product_id if body.product_id is not None else str(existing["product_id"])
            old_status = existing["status"]
            new_status = body.status if body.status is not None else old_status

            if new_status != old_status:
                if old_status == "REPLACED":
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A replaced unit's status can't be changed")
                if new_status not in _MANUAL_STATUSES:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Status {new_status} is set by the sell / claim / replace actions, not by editing",
                    )
                if old_status == "SOLD":
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This unit is with a customer — file a claim first")
                if new_status == "IN_STOCK" and existing["invoice_id"] is not None:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This unit was sold to a customer, so it can't go back in stock")

            if product_id != str(existing["product_id"]):
                if existing["invoice_id"] is not None:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A unit that was sold can't be moved to another product")
                if not await conn.fetchval("SELECT 1 FROM products WHERE id = $1::uuid", product_id):
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

            supplier_months = (body.supplier_warranty_months if body.supplier_warranty_months is not None
                               else existing["supplier_warranty_months"])
            supplier_until = body.supplier_warranty_valid_until
            if supplier_until is None:
                if body.supplier_warranty_months is not None and existing["purchase_date"]:
                    supplier_until = add_months(existing["purchase_date"], body.supplier_warranty_months)
                else:
                    supplier_until = existing["supplier_warranty_valid_until"]

            customer_months = (body.customer_warranty_months if body.customer_warranty_months is not None
                               else existing["customer_warranty_months"])
            customer_until = body.customer_warranty_valid_until
            if customer_until is None:
                if body.customer_warranty_months is not None and existing["sale_date"]:
                    customer_until = add_months(existing["sale_date"], body.customer_warranty_months)
                else:
                    customer_until = existing["customer_warranty_valid_until"]

            notes = body.notes if body.notes is not None else existing["notes"]

            try:
                await conn.execute(
                    """UPDATE part_warranties SET barcode=$1, product_id=$2::uuid, supplier_warranty_months=$3,
                              supplier_warranty_valid_until=$4, customer_warranty_months=$5,
                              customer_warranty_valid_until=$6, status=$7, notes=$8, updated_at=now()
                       WHERE id=$9""",
                    barcode, product_id, supplier_months, supplier_until, customer_months, customer_until,
                    new_status, notes, warranty_id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This barcode is already tracked")

            # Keep the stock ledger in step with the unit physically leaving / re-entering stock.
            was_in_stock = old_status == "IN_STOCK"
            will_be_in_stock = new_status == "IN_STOCK"
            old_product = str(existing["product_id"])
            note = f"Unit {barcode or warranty_id}"
            if was_in_stock and not will_be_in_stock:
                await conn.execute(
                    """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                       VALUES ($1::uuid, -1, $2, $3::uuid, $4, $5::uuid)""",
                    old_product, "RETURN_OUT" if new_status == "RETURNED" else "ADJUSTMENT", warranty_id,
                    f"{note} marked {new_status}", current_user["id"],
                )
            elif not was_in_stock and will_be_in_stock:
                await conn.execute(
                    """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                       VALUES ($1::uuid, 1, 'RETURN_IN', $2::uuid, $3, $4::uuid)""",
                    product_id, warranty_id, f"{note} back in stock", current_user["id"],
                )
            elif was_in_stock and will_be_in_stock and product_id != old_product:
                await conn.execute(
                    """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                       VALUES ($1::uuid, -1, 'ADJUSTMENT', $3::uuid, $4, $5::uuid),
                              ($2::uuid,  1, 'ADJUSTMENT', $3::uuid, $4, $5::uuid)""",
                    old_product, product_id, warranty_id, f"{note} moved to another product", current_user["id"],
                )
            row = await conn.fetchrow(f"{_SELECT} WHERE w.id = $1", warranty_id)
    return row_to_dict(row)


@router.post("/{warranty_id}/sell")
async def sell_warranty(warranty_id: str, body: SellWarrantyRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM part_warranties WHERE id = $1", warranty_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if existing["status"] == "SOLD":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This unit is already marked as sold")

        sale_date = body.sale_date
        valid_until = body.customer_warranty_valid_until
        if valid_until is None and body.customer_warranty_months and sale_date:
            valid_until = add_months(sale_date, body.customer_warranty_months)

        row = await conn.fetchrow(
            """UPDATE part_warranties SET status='SOLD', invoice_id=$1, invoice_item_id=$2,
                      sale_date=COALESCE($3, CURRENT_DATE), customer_warranty_months=$4,
                      customer_warranty_valid_until=$5, updated_at=now()
               WHERE id=$6 RETURNING *""",
            body.invoice_id, body.invoice_item_id, sale_date, body.customer_warranty_months,
            valid_until, warranty_id,
        )
    return row_to_dict(row)


async def _perform_replacement(conn: asyncpg.Connection, original: asyncpg.Record, replacement_barcode: str) -> str:
    """Swap `original` (a claimed unit) for an IN_STOCK unit with `replacement_barcode`.
    The replacement takes over the original's invoice link and remaining customer warranty
    period (it doesn't restart the clock); the original is marked REPLACED and the two are
    cross-linked so the customer/supplier history views show the full trail."""
    if original["invoice_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This unit was never sold to a customer, so there's nothing to replace it on",
        )
    replacement = await conn.fetchrow(
        "SELECT * FROM part_warranties WHERE product_id = $1 AND barcode = $2 FOR UPDATE",
        original["product_id"], replacement_barcode,
    )
    if replacement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Barcode '{replacement_barcode}' isn't a tracked unit for this product — record its purchase first",
        )
    if replacement["status"] != "IN_STOCK":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Replacement barcode is not available (status: {replacement['status']})",
        )
    await conn.execute(
        """UPDATE part_warranties SET status='SOLD', invoice_id=$1, invoice_item_id=$2, sale_date=CURRENT_DATE,
                  customer_warranty_months=$3, customer_warranty_valid_until=$4, replaces_warranty_id=$5, updated_at=now()
           WHERE id=$6""",
        original["invoice_id"], original["invoice_item_id"], original["customer_warranty_months"],
        original["customer_warranty_valid_until"], original["id"], replacement["id"],
    )
    await conn.execute(
        "UPDATE part_warranties SET status='REPLACED', replaced_by_warranty_id=$1, updated_at=now() WHERE id=$2",
        replacement["id"], original["id"],
    )
    return replacement["id"]


@router.post("/{warranty_id}/claim")
async def claim_warranty(warranty_id: str, body: ClaimWarrantyRequest):
    """File a warranty claim on a sold unit. Pass replacement_barcode too if you're handing
    the customer a replacement on the spot (otherwise use /replace once one is available)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            original = await conn.fetchrow("SELECT * FROM part_warranties WHERE id = $1 FOR UPDATE", warranty_id)
            if original is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if original["status"] not in ("SOLD", "CLAIMED"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Can't file a claim on a unit that's {original['status']}",
                )
            await conn.execute(
                "UPDATE part_warranties SET status='CLAIMED', notes=COALESCE($1, notes), updated_at=now() WHERE id=$2",
                body.notes, warranty_id,
            )
            if body.replacement_barcode:
                await _perform_replacement(conn, original, body.replacement_barcode)
            row = await conn.fetchrow(f"{_SELECT} WHERE w.id = $1", warranty_id)
    return row_to_dict(row)


@router.post("/{warranty_id}/replace")
async def replace_warranty(warranty_id: str, body: ReplaceWarrantyRequest):
    """Swap a previously-claimed unit for a replacement once one is available."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            original = await conn.fetchrow("SELECT * FROM part_warranties WHERE id = $1 FOR UPDATE", warranty_id)
            if original is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if original["status"] != "CLAIMED":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Only a claimed unit can be replaced — file a claim first",
                )
            if body.notes:
                await conn.execute(
                    "UPDATE part_warranties SET notes = $1, updated_at = now() WHERE id = $2",
                    body.notes, warranty_id,
                )
            await _perform_replacement(conn, original, body.replacement_barcode)
            row = await conn.fetchrow(f"{_SELECT} WHERE w.id = $1", warranty_id)
    return row_to_dict(row)


@router.delete("/{warranty_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_warranty(warranty_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM part_warranties WHERE id = $1", warranty_id)
    return None
