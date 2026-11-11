import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.products import (
    CreateCategoryRequest,
    CreateProductRequest,
    StockAdjustRequest,
    UpdateProductRequest,
)
from app.security import get_current_user, require_role
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.audit import AuditRoute

router = APIRouter(route_class=AuditRoute, prefix="/products", tags=["products"], dependencies=[Depends(require_role("OWNER"))])


@router.get("")
async def list_products(search: SearchTerm = None, low_stock: bool | None = None, page: Page = Depends()):
    pool = get_pool()
    having_clause = "AND p.current_stock <= p.reorder_level" if low_stock else ""
    query = f"""
        SELECT p.*, c.name AS category_name,
               COUNT(*) OVER() AS _total
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.is_active = TRUE
          AND ($1::text IS NULL OR p.name ILIKE '%'||$1||'%' ESCAPE '\\' OR p.sku ILIKE '%'||$1||'%' ESCAPE '\\')
        {having_clause}
        ORDER BY p.name, p.id
        LIMIT $2 OFFSET $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, search, page.limit, page.offset)
    return paged_response(rows)


@router.get("/meta/categories")
async def list_categories():
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM categories ORDER BY name")
    return rows_to_list(rows)


@router.post("/meta/categories", status_code=status.HTTP_201_CREATED)
async def create_category(body: CreateCategoryRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO categories (name) VALUES ($1)
               ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING *""",
            body.name,
        )
    return row_to_dict(row)


@router.get("/{product_id}")
async def get_product(product_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM products WHERE id = $1", product_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row_to_dict(row)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product(body: CreateProductRequest):
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO products (sku, name, category_id, brand, hsn_code, unit, gst_rate, purchase_price, selling_price, reorder_level, is_serialized)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
                body.sku, body.name, body.category_id, body.brand, body.hsn_code,
                body.unit, body.gst_rate, body.purchase_price, body.selling_price, body.reorder_level, body.is_serialized,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A product with this SKU already exists")
    return row_to_dict(row)


@router.put("/{product_id}")
async def update_product(product_id: str, body: UpdateProductRequest):
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE products SET sku=$1, name=$2, category_id=$3, brand=$4, hsn_code=$5, unit=$6, gst_rate=$7,
                          selling_price=$8, reorder_level=$9, is_serialized=$10,
                          purchase_price=COALESCE($11, purchase_price)
                   WHERE id=$12 AND is_active = TRUE RETURNING *""",
                body.sku, body.name, body.category_id, body.brand, body.hsn_code,
                body.unit, body.gst_rate, body.selling_price, body.reorder_level, body.is_serialized,
                body.purchase_price, product_id,
            )
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            category_name = None
            if row["category_id"]:
                category_name = await conn.fetchval("SELECT name FROM categories WHERE id = $1", row["category_id"])
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A product with this SKU already exists")
    result = row_to_dict(row)
    result["category_name"] = category_name
    return result


@router.post("/{product_id}/adjust-stock", status_code=status.HTTP_201_CREATED)
async def adjust_stock(product_id: str, body: StockAdjustRequest, current_user=Depends(get_current_user)):
    if body.quantity == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quantity can't be zero")
    try:
        product_id = str(uuid.UUID(product_id))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            product = await conn.fetchrow("SELECT name, current_stock FROM products WHERE id = $1 FOR UPDATE", product_id)
            if product is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if product["current_stock"] + body.quantity < 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Can't remove {abs(body.quantity):g} of {product['name']}: only {product['current_stock']:g} in stock",
                )
            await conn.execute(
                """INSERT INTO stock_ledger (product_id, quantity, reference_type, note, created_by)
                   VALUES ($1,$2,'ADJUSTMENT',$3,$4)""",
                product_id, body.quantity, body.note, current_user["id"],
            )
    return {"ok": True}


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "UPDATE products SET is_active = FALSE WHERE id = $1 AND is_active = TRUE RETURNING id", product_id
        )
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return None
