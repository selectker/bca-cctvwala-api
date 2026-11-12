"""Business expenses (OWNER only): rent, salary, electricity, travel...

They feed GET /reports/profit-loss. Every create / change / delete is written to the audit log with the amounts,
because this is money: a deleted expense still leaves a row saying what it was and who removed it.
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.db import get_pool
from app.schemas.expenses import ExpenseRequest
from app.security import require_role
from app.utils.audit import log_event
from app.utils.dates import parse_date, today_local
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict
from app.utils.search import SearchTerm

router = APIRouter(prefix="/expenses", tags=["expenses"], dependencies=[Depends(require_role("OWNER"))])

# Suggested categories. Free text is allowed too (rows keep what was typed), but a name that matches one of these or
# one already used, ignoring case, is saved with that spelling so "rent" and "Rent" never become two lines in a report.
DEFAULT_CATEGORIES = (
    "Rent", "Salary & wages", "Electricity & water", "Internet & phone", "Travel & fuel", "Vehicle & transport",
    "Repairs & maintenance", "Office & stationery", "Marketing", "Bank & payment charges", "Taxes & fees",
    "Insurance", "Professional fees", "Miscellaneous",
)

_COLUMNS = """e.id, e.expense_date, e.category, e.amount, e.payment_mode, e.paid_to, e.reference_no, e.notes,
              e.created_at, e.updated_at, u.name AS created_by_name"""
_FROM = "expenses e LEFT JOIN users u ON u.id = e.created_by"


def _money(value) -> str:
    return f"₹{Decimal(value):,.2f}"


async def _canonical_category(conn, typed: str) -> str:
    """The existing spelling of `typed` (ignoring case and extra spaces), or `typed` tidied up."""
    tidy = " ".join(typed.split())
    for name in DEFAULT_CATEGORIES:
        if name.lower() == tidy.lower():
            return name
    used = await conn.fetchval("SELECT category FROM expenses WHERE lower(category) = lower($1) ORDER BY created_at LIMIT 1", tidy)
    return used or tidy


def _blank_to_none(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


@router.get("")
async def list_expenses(
    from_: str = Query(default=None, alias="from"),
    to: str = None,
    category: str = Query(default=None, max_length=60),
    search: SearchTerm = None,
    page: Page = Depends(),
):
    """Newest first. X-Total-Count is the number of matches and X-Total-Amount their sum (not just this page's)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT {_COLUMNS}, COUNT(*) OVER() AS _total, COALESCE(SUM(e.amount) OVER(), 0) AS _sum
                FROM {_FROM}
                WHERE ($1::date IS NULL OR e.expense_date >= $1) AND ($2::date IS NULL OR e.expense_date <= $2)
                  AND ($3::text IS NULL OR lower(e.category) = lower($3))
                  AND ($4::text IS NULL OR e.paid_to ILIKE '%'||$4||'%' ESCAPE '\\' OR e.notes ILIKE '%'||$4||'%' ESCAPE '\\'
                       OR e.category ILIKE '%'||$4||'%' ESCAPE '\\' OR e.reference_no ILIKE '%'||$4||'%' ESCAPE '\\')
                ORDER BY e.expense_date DESC, e.created_at DESC, e.id LIMIT $5 OFFSET $6""",
            parse_date(from_), parse_date(to), category, search, page.limit, page.offset,
        )
    total_amount = rows[0]["_sum"] if rows else Decimal("0")
    return paged_response(rows, extra_drop=("_sum",), extra_headers={"X-Total-Amount": str(total_amount)})


@router.get("/categories")
async def list_categories():
    """The suggested categories plus any others already used, A to Z."""
    pool = get_pool()
    async with pool.acquire() as conn:
        used = [r["category"] for r in await conn.fetch("SELECT DISTINCT category FROM expenses")]
    names = {n.lower(): n for n in DEFAULT_CATEGORIES}
    for name in used:
        names.setdefault(name.lower(), name)
    return sorted(names.values(), key=str.lower)


@router.get("/summary")
async def expense_summary(from_: str = Query(default=None, alias="from"), to: str = None):
    """Total, count, the total per category (biggest first) and per month for [from, to]."""
    if not from_ or not to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to (YYYY-MM-DD) required")
    start, end = parse_date(from_), parse_date(to)
    pool = get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchrow(
            "SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total FROM expenses WHERE expense_date BETWEEN $1 AND $2",
            start, end,
        )
        by_category = await conn.fetch(
            """SELECT MIN(category) AS category, SUM(amount) AS total, COUNT(*) AS count FROM expenses
               WHERE expense_date BETWEEN $1 AND $2 GROUP BY lower(category) ORDER BY SUM(amount) DESC, lower(category)""",
            start, end,
        )
        by_month = await conn.fetch(
            """SELECT to_char(date_trunc('month', expense_date), 'YYYY-MM') AS month, SUM(amount) AS total FROM expenses
               WHERE expense_date BETWEEN $1 AND $2 GROUP BY 1 ORDER BY 1""",
            start, end,
        )
    return jsonable_encoder({
        "count": total["count"], "total": total["total"],
        "by_category": [dict(r) for r in by_category], "by_month": [dict(r) for r in by_month],
    })


@router.get("/{expense_id}")
async def get_expense(expense_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM {_FROM} WHERE e.id = $1", expense_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row_to_dict(row)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_expense(body: ExpenseRequest, request: Request, current_user=Depends(require_role("OWNER"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        category = await _canonical_category(conn, body.category)
        new_id = await conn.fetchval(
            """INSERT INTO expenses (expense_date, category, amount, payment_mode, paid_to, reference_no, notes, created_by)
               VALUES (COALESCE($1, CURRENT_DATE), $2, $3, $4, $5, $6, $7, $8::uuid) RETURNING id""",
            body.expense_date or today_local(), category, body.amount, body.payment_mode,
            _blank_to_none(body.paid_to), _blank_to_none(body.reference_no), _blank_to_none(body.notes), str(current_user["id"]),
        )
        row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM {_FROM} WHERE e.id = $1", new_id)
    await log_event(
        current_user, "CREATE", "EXPENSE", new_id, request=request, display=category,
        summary=f"Created expense {category} {_money(row['amount'])}",
        details={"expense_date": row["expense_date"].isoformat(), "category": category, "amount": str(row["amount"]),
                 "payment_mode": row["payment_mode"], "paid_to": row["paid_to"]},
    )
    return row_to_dict(row)


@router.put("/{expense_id}")
async def update_expense(expense_id: str, body: ExpenseRequest, request: Request, current_user=Depends(require_role("OWNER"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        before = await conn.fetchrow(f"SELECT {_COLUMNS} FROM {_FROM} WHERE e.id = $1", expense_id)
        if before is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        category = await _canonical_category(conn, body.category)
        await conn.execute(
            """UPDATE expenses SET expense_date = $2, category = $3, amount = $4, payment_mode = $5, paid_to = $6,
                      reference_no = $7, notes = $8, updated_at = now() WHERE id = $1""",
            expense_id, body.expense_date or before["expense_date"], category, body.amount, body.payment_mode,
            _blank_to_none(body.paid_to), _blank_to_none(body.reference_no), _blank_to_none(body.notes),
        )
        row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM {_FROM} WHERE e.id = $1", expense_id)
    changes = {
        key: {"from": str(before[key]), "to": str(row[key])}
        for key in ("expense_date", "category", "amount", "payment_mode", "paid_to", "reference_no", "notes")
        if before[key] != row[key]
    }
    note = f" ({_money(before['amount'])} to {_money(row['amount'])})" if before["amount"] != row["amount"] else ""
    await log_event(
        current_user, "UPDATE", "EXPENSE", expense_id, request=request, display=category,
        summary=f"Updated expense {category}{note}", details={"changes": changes},
    )
    return row_to_dict(row)


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(expense_id: str, request: Request, current_user=Depends(require_role("OWNER"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM {_FROM} WHERE e.id = $1", expense_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        await conn.execute("DELETE FROM expenses WHERE id = $1", expense_id)
    await log_event(
        current_user, "DELETE", "EXPENSE", expense_id, request=request, display=row["category"],
        summary=f"Deleted expense {row['category']} {_money(row['amount'])} dated {row['expense_date'].isoformat()}",
        details={"expense_date": row["expense_date"].isoformat(), "category": row["category"], "amount": str(row["amount"]),
                 "payment_mode": row["payment_mode"], "paid_to": row["paid_to"], "notes": row["notes"]},
    )
    return None
