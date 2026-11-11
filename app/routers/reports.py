import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.config import settings
from app.db import get_pool
from app.security import get_current_user, require_role
from app.utils.dates import parse_date, today_local
from app.utils import gstr
from app.utils.gst import get_store_state_code, round2
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list

router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(require_role("OWNER"))])


@router.get("/gst-summary")
async def gst_summary(from_: str = Query(default=None, alias="from"), to: str = None):  # validated below to mirror Node's 400 behavior
    if not from_ or not to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to (YYYY-MM-DD) required")
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COUNT(*) AS invoice_count,
                      COALESCE(SUM(subtotal),0) AS total_taxable,
                      COALESCE(SUM(total_cgst),0) AS total_cgst,
                      COALESCE(SUM(total_sgst),0) AS total_sgst,
                      COALESCE(SUM(total_igst),0) AS total_igst,
                      COALESCE(SUM(total_amount),0) AS total_invoice_value
               FROM invoices WHERE invoice_date BETWEEN $1 AND $2 AND status = 'ACTIVE'""",
            parse_date(from_), parse_date(to),
        )
        cn = await conn.fetchrow(
            """SELECT COUNT(*) AS n, COALESCE(SUM(subtotal),0) AS taxable, COALESCE(SUM(total_cgst),0) AS cgst,
                      COALESCE(SUM(total_sgst),0) AS sgst, COALESCE(SUM(total_igst),0) AS igst,
                      COALESCE(SUM(total_amount),0) AS total
               FROM credit_notes WHERE note_date BETWEEN $1 AND $2 AND status = 'ACTIVE'""",
            parse_date(from_), parse_date(to),
        )
    result = row_to_dict(row)
    # Credit notes issued in the period are taken off (that is how GSTR-3B nets them); the figures shown are net.
    result.update(
        credit_note_count=cn["n"], credit_note_value=cn["total"],
        gross_taxable=result["total_taxable"], gross_invoice_value=result["total_invoice_value"],
        total_taxable=result["total_taxable"] - cn["taxable"], total_cgst=result["total_cgst"] - cn["cgst"],
        total_sgst=result["total_sgst"] - cn["sgst"], total_igst=result["total_igst"] - cn["igst"],
        total_invoice_value=result["total_invoice_value"] - cn["total"],
    )
    return result


@router.get("/gst-input-credit")
async def gst_input_credit(from_: str = Query(default=None, alias="from"), to: str = None):
    if not from_ or not to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to (YYYY-MM-DD) required")
    pool = get_pool()
    async with pool.acquire() as conn:
        store_state = await get_store_state_code(conn)
        # Purchases only store a single total_gst. The CGST/SGST/IGST split is derived the same way
        # sales are taxed: supplier in another state -> IGST, otherwise half CGST + half SGST.
        row = await conn.fetchrow(
            """SELECT COUNT(*) AS purchase_count,
                      COALESCE(SUM(p.subtotal),0) AS total_taxable,
                      COALESCE(SUM(p.total_gst),0) AS total_gst_paid,
                      COALESCE(SUM(p.total_amount),0) AS total_purchase_value,
                      COALESCE(SUM(CASE WHEN s.state_code IS NOT NULL AND s.state_code <> $3
                                        THEN 0 ELSE ROUND(p.total_gst / 2, 2) END),0) AS total_cgst,
                      COALESCE(SUM(CASE WHEN s.state_code IS NOT NULL AND s.state_code <> $3
                                        THEN 0 ELSE p.total_gst - ROUND(p.total_gst / 2, 2) END),0) AS total_sgst,
                      COALESCE(SUM(CASE WHEN s.state_code IS NOT NULL AND s.state_code <> $3
                                        THEN p.total_gst ELSE 0 END),0) AS total_igst
               FROM purchases p JOIN suppliers s ON s.id = p.supplier_id
               WHERE p.purchase_date BETWEEN $1 AND $2 AND p.status = 'ACTIVE'""",
            parse_date(from_), parse_date(to), store_state,
        )
        dn = await conn.fetchrow(
            """SELECT COUNT(*) AS n, COALESCE(SUM(d.subtotal),0) AS taxable, COALESCE(SUM(d.total_gst),0) AS gst,
                      COALESCE(SUM(d.total_amount),0) AS total,
                      COALESCE(SUM(CASE WHEN s.state_code IS NOT NULL AND s.state_code <> $3
                                        THEN 0 ELSE ROUND(d.total_gst / 2, 2) END),0) AS cgst,
                      COALESCE(SUM(CASE WHEN s.state_code IS NOT NULL AND s.state_code <> $3
                                        THEN 0 ELSE d.total_gst - ROUND(d.total_gst / 2, 2) END),0) AS sgst,
                      COALESCE(SUM(CASE WHEN s.state_code IS NOT NULL AND s.state_code <> $3
                                        THEN d.total_gst ELSE 0 END),0) AS igst
               FROM debit_notes d JOIN suppliers s ON s.id = d.supplier_id
               WHERE d.note_date BETWEEN $1 AND $2 AND d.status = 'ACTIVE'""",
            parse_date(from_), parse_date(to), store_state,
        )
    result = row_to_dict(row)
    # Debit notes (goods sent back) in the period reduce the input credit; the figures shown are net.
    result.update(
        debit_note_count=dn["n"], debit_note_value=dn["total"],
        total_taxable=result["total_taxable"] - dn["taxable"], total_gst_paid=result["total_gst_paid"] - dn["gst"],
        total_purchase_value=result["total_purchase_value"] - dn["total"], total_cgst=result["total_cgst"] - dn["cgst"],
        total_sgst=result["total_sgst"] - dn["sgst"], total_igst=result["total_igst"] - dn["igst"],
    )
    return result


@router.get("/profit-loss")
async def profit_loss(from_: str = Query(default=None, alias="from"), to: str = None):
    """Profit for [from, to]: sales (before GST) - what the goods cost - expenses.

    * sales_taxable: the invoices' value before GST (the same figure as `total_taxable` in gst-summary). Cancelled
      invoices are left out.
    * cost_of_goods: for every product line sold, quantity x the product's `purchase_price` (ex-GST, kept up to date by
      purchases). It is an ESTIMATE: invoices don't store the cost at the moment of sale, so a product bought dearer or
      cheaper since is costed at its latest price. A custom line (no product) costs its own optional `cost_price` x quantity, or nothing when no cost was entered.
      `lines_without_cost` counts product lines whose purchase_price is 0 (their cost is unknown, so profit is overstated).
    * expenses_total: the Expenses recorded for the period (amount paid, including any GST).
    Credit notes dated in the period are taken off the sales; goods that came back into stock are taken off the cost too
    (`credit_note_taxable` shows the amount netted).
    """
    if not from_ or not to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to (YYYY-MM-DD) required")
    start, end = parse_date(from_), parse_date(to)
    pool = get_pool()
    async with pool.acquire() as conn:
        sales = await conn.fetchrow(
            """SELECT COUNT(*) AS invoice_count, COALESCE(SUM(subtotal), 0) AS sales_taxable
               FROM invoices WHERE invoice_date BETWEEN $1 AND $2 AND status = 'ACTIVE'""",
            start, end,
        )
        cost = await conn.fetchrow(
            """SELECT COALESCE(SUM(ii.quantity * COALESCE(CASE WHEN ii.product_id IS NULL THEN ii.cost_price ELSE p.purchase_price END, 0)), 0) AS cost_of_goods,
                      COUNT(*) FILTER (WHERE ii.product_id IS NOT NULL AND COALESCE(p.purchase_price, 0) = 0) AS lines_without_cost
               FROM invoice_items ii
               JOIN invoices i ON i.id = ii.invoice_id
               LEFT JOIN products p ON p.id = ii.product_id
               WHERE i.invoice_date BETWEEN $1 AND $2 AND i.status = 'ACTIVE'""",
            start, end,
        )
        returns = await conn.fetchrow(
            """SELECT COALESCE(SUM(n.subtotal), 0) AS taxable,
                      COALESCE((SELECT SUM(ni.quantity * COALESCE(CASE WHEN ni.product_id IS NULL THEN ii.cost_price ELSE p.purchase_price END, 0))
                                FROM credit_note_items ni JOIN credit_notes n2 ON n2.id = ni.credit_note_id
                                JOIN invoice_items ii ON ii.id = ni.invoice_item_id
                                LEFT JOIN products p ON p.id = ni.product_id
                                WHERE n2.note_date BETWEEN $1 AND $2 AND n2.status = 'ACTIVE' AND n2.restock), 0) AS restocked_cost
               FROM credit_notes n WHERE n.note_date BETWEEN $1 AND $2 AND n.status = 'ACTIVE'""",
            start, end,
        )
        spent = await conn.fetchrow(
            "SELECT COUNT(*) AS expense_count, COALESCE(SUM(amount), 0) AS expenses_total FROM expenses WHERE expense_date BETWEEN $1 AND $2",
            start, end,
        )
        by_category = await conn.fetch(
            """SELECT MIN(category) AS category, SUM(amount) AS total FROM expenses WHERE expense_date BETWEEN $1 AND $2
               GROUP BY lower(category) ORDER BY SUM(amount) DESC, lower(category) LIMIT 8""",
            start, end,
        )
    taxable = sales["sales_taxable"] - returns["taxable"]
    goods = cost["cost_of_goods"] - returns["restocked_cost"]
    expenses = spent["expenses_total"]
    gross = taxable - goods
    net = gross - expenses

    def percent(part):
        return float(round(part * 100 / taxable, 1)) if taxable else 0.0

    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "invoice_count": sales["invoice_count"],
        "sales_taxable": float(taxable),
        "credit_note_taxable": float(returns["taxable"]),
        "cost_of_goods": float(goods),
        "gross_profit": float(gross),
        "gross_margin_percent": percent(gross),
        "expense_count": spent["expense_count"],
        "expenses_total": float(expenses),
        "expenses_by_category": [{"category": r["category"], "total": float(r["total"])} for r in by_category],
        "net_profit": float(net),
        "net_margin_percent": percent(net),
        "lines_without_cost": cost["lines_without_cost"],
        "cost_is_estimate": True,
    }


MAX_SALES_SUMMARY_DAYS = 3660


@router.get("/sales-summary")
async def sales_summary(from_: str = Query(default=None, alias="from"), to: str = None):
    """One row per calendar day in [from, to] (sales net of that day's credit notes) — days with no invoices come back as zeros, so charts
    don't have to fill gaps themselves."""
    if not from_ or not to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to required")
    start, end = parse_date(from_), parse_date(to)
    if end < start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from must not be after to")
    if (end - start).days >= MAX_SALES_SUMMARY_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Date range too large (max 10 years)")
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT d.day::date AS date, COUNT(i.id)::int AS invoice_count,
                      COALESCE(SUM(i.total_amount),0)
                        - COALESCE((SELECT SUM(n.total_amount) FROM credit_notes n
                                    WHERE n.note_date = d.day::date AND n.status = 'ACTIVE'), 0) AS total_sales
               FROM generate_series($1::date, $2::date, INTERVAL '1 day') AS d(day)
               LEFT JOIN invoices i ON i.invoice_date = d.day::date AND i.status = 'ACTIVE'
               GROUP BY d.day ORDER BY d.day""",
            start, end,
        )
    return rows_to_list(rows)


@router.get("/top-products")
async def top_products(from_: str = Query(default=None, alias="from"), to: str = None, limit: int = Query(20, ge=1, le=100)):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.name, SUM(ii.quantity) AS qty_sold, SUM(ii.line_total) AS revenue
               FROM invoice_items ii
               JOIN invoices i ON i.id = ii.invoice_id
               JOIN products p ON p.id = ii.product_id
               WHERE i.status = 'ACTIVE' AND ($1::date IS NULL OR i.invoice_date >= $1) AND ($2::date IS NULL OR i.invoice_date <= $2)
               GROUP BY p.name ORDER BY revenue DESC LIMIT $3""",
            parse_date(from_), parse_date(to), limit,
        )
    return rows_to_list(rows)


@router.get("/low-stock")
async def low_stock(page: Page = Depends()):
    """Paged. X-Total-Count = items at/below reorder level; X-Out-Of-Stock-Count = how many of
    those (across all pages, not just this one) are at zero or below."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, name, sku, reorder_level, current_stock,
                      COUNT(*) OVER() AS _total,
                      COUNT(*) FILTER (WHERE current_stock <= 0) OVER() AS _out
               FROM products
               WHERE is_active = TRUE AND current_stock <= reorder_level
               ORDER BY current_stock, name, id
               LIMIT $1 OFFSET $2""",
            page.limit, page.offset,
        )
    out = int(rows[0]["_out"]) if rows else 0
    return paged_response(rows, extra_drop=("_out",), extra_headers={"X-Out-Of-Stock-Count": str(out)})


@router.get("/outstanding-receivables")
async def outstanding_receivables(page: Page = Depends()):
    """Paged, highest balance first. X-Total-Outstanding = the sum across every matching customer."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """WITH d AS (
                   SELECT c.id, c.name, c.phone, c.opening_balance,
                          COALESCE(SUM(i.total_amount - i.paid_amount),0) AS invoice_due,
                          c.opening_balance + COALESCE(SUM(i.total_amount - i.paid_amount),0) AS total_outstanding
                   FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.payment_status <> 'PAID' AND i.status = 'ACTIVE'
                   GROUP BY c.id HAVING c.opening_balance + COALESCE(SUM(i.total_amount - i.paid_amount),0) > 0
               )
               SELECT d.*, COUNT(*) OVER() AS _total, SUM(d.total_outstanding) OVER() AS _sum
               FROM d ORDER BY d.total_outstanding DESC, d.id
               LIMIT $1 OFFSET $2""",
            page.limit, page.offset,
        )
    total_sum = float(rows[0]["_sum"]) if rows else 0.0
    return paged_response(rows, extra_drop=("_sum",), extra_headers={"X-Total-Outstanding": str(total_sum)})


@router.get("/receivables-ageing")
async def receivables_ageing(page: Page = Depends()):
    """Who owes what, and for how long past the due date. One row per customer who owes money, biggest balance first:
    `not_due`, `days_1_30`, `days_31_60`, `days_61_90`, `days_over_90` split the unpaid live invoices by how many days
    they are past due (an invoice without a due date is due on its own date); `opening_balance` is the amount carried
    over from before the app and has no age. `overdue_amount` = everything past due; `total_outstanding` = all of it.
    X-Total-Outstanding is the sum across every customer, X-Total-Overdue the overdue part of it."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """WITH inv AS (
                   SELECT i.customer_id, i.total_amount - i.paid_amount AS due,
                          CURRENT_DATE - COALESCE(i.due_date, i.invoice_date) AS late
                   FROM invoices i WHERE i.status = 'ACTIVE' AND i.payment_status <> 'PAID'
               ), d AS (
                   SELECT c.id, c.name, c.phone, c.credit_limit, c.opening_balance,
                          COALESCE(SUM(inv.due) FILTER (WHERE inv.late <= 0), 0) AS not_due,
                          COALESCE(SUM(inv.due) FILTER (WHERE inv.late BETWEEN 1 AND 30), 0) AS days_1_30,
                          COALESCE(SUM(inv.due) FILTER (WHERE inv.late BETWEEN 31 AND 60), 0) AS days_31_60,
                          COALESCE(SUM(inv.due) FILTER (WHERE inv.late BETWEEN 61 AND 90), 0) AS days_61_90,
                          COALESCE(SUM(inv.due) FILTER (WHERE inv.late > 90), 0) AS days_over_90,
                          COALESCE(SUM(inv.due) FILTER (WHERE inv.late > 0), 0) AS overdue_amount,
                          COALESCE(SUM(inv.due), 0) AS invoice_due
                   FROM customers c LEFT JOIN inv ON inv.customer_id = c.id
                   GROUP BY c.id
               ), t AS (
                   SELECT d.*, d.opening_balance + d.invoice_due AS total_outstanding FROM d
                   WHERE d.opening_balance + d.invoice_due > 0
               )
               SELECT t.*, COUNT(*) OVER() AS _total, SUM(t.total_outstanding) OVER() AS _sum,
                      SUM(t.overdue_amount) OVER() AS _overdue
               FROM t ORDER BY t.total_outstanding DESC, t.id
               LIMIT $1 OFFSET $2""",
            page.limit, page.offset,
        )
    total_sum = float(rows[0]["_sum"]) if rows else 0.0
    total_overdue = float(rows[0]["_overdue"]) if rows else 0.0
    return paged_response(rows, extra_drop=("_sum", "_overdue"),
                          extra_headers={"X-Total-Outstanding": str(total_sum), "X-Total-Overdue": str(total_overdue)})


@router.get("/outstanding-payables")
async def outstanding_payables(page: Page = Depends()):
    """Paged, highest balance first. X-Total-Outstanding = the sum across every matching supplier."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """WITH d AS (
                   SELECT s.id, s.name, s.phone, s.opening_balance,
                          COALESCE(SUM(p.total_amount - p.paid_amount),0) AS purchase_due,
                          s.opening_balance + COALESCE(SUM(p.total_amount - p.paid_amount),0) AS total_outstanding
                   FROM suppliers s LEFT JOIN purchases p ON p.supplier_id = s.id AND p.payment_status <> 'PAID' AND p.status = 'ACTIVE'
                   GROUP BY s.id HAVING s.opening_balance + COALESCE(SUM(p.total_amount - p.paid_amount),0) > 0
               )
               SELECT d.*, COUNT(*) OVER() AS _total, SUM(d.total_outstanding) OVER() AS _sum
               FROM d ORDER BY d.total_outstanding DESC, d.id
               LIMIT $1 OFFSET $2""",
            page.limit, page.offset,
        )
    total_sum = float(rows[0]["_sum"]) if rows else 0.0
    return paged_response(rows, extra_drop=("_sum",), extra_headers={"X-Total-Outstanding": str(total_sum)})


@router.get("/dashboard")
async def dashboard():
    pool = get_pool()

    # One connection per query: asyncpg connections run a single operation at a time, so the
    # concurrent gather() below must not share one.
    async def run(method: str, query: str, *args):
        async with pool.acquire() as conn:
            return await getattr(conn, method)(query, *args)

    (
        today_sales, month_sales, prev_month_sales, receivables, payables, low_stock_count, amc_due,
        trend_rows, recent_rows, top_rows, low_rows, unpaid, ticket_stats, ticket_review_rows,
    ) = await asyncio.gather(
        run("fetchrow", "SELECT COALESCE(SUM(total_amount),0) AS total, COUNT(*)::int AS count FROM invoices WHERE status = 'ACTIVE' AND invoice_date = CURRENT_DATE"),
        run("fetchrow", "SELECT COALESCE(SUM(total_amount),0) AS total, COUNT(*)::int AS count FROM invoices WHERE status = 'ACTIVE' AND date_trunc('month', invoice_date) = date_trunc('month', CURRENT_DATE)"),
        run(
            "fetchrow",
            """SELECT COALESCE(SUM(total_amount),0) AS total, COUNT(*)::int AS count FROM invoices
               WHERE status = 'ACTIVE' AND date_trunc('month', invoice_date) = date_trunc('month', CURRENT_DATE - INTERVAL '1 month')""",
        ),
        run(
            "fetchrow",
            """SELECT COALESCE(SUM(COALESCE(c.opening_balance,0) + COALESCE(sub.due,0)),0) AS total FROM customers c
               LEFT JOIN (SELECT customer_id, SUM(total_amount-paid_amount) AS due FROM invoices WHERE status = 'ACTIVE' GROUP BY customer_id) sub
               ON sub.customer_id = c.id WHERE c.is_active = TRUE""",
        ),
        run(
            "fetchrow",
            """SELECT COALESCE(SUM(COALESCE(s.opening_balance,0) + COALESCE(sub.due,0)),0) AS total FROM suppliers s
               LEFT JOIN (SELECT supplier_id, SUM(total_amount-paid_amount) AS due FROM purchases WHERE status = 'ACTIVE' GROUP BY supplier_id) sub
               ON sub.supplier_id = s.id WHERE s.is_active = TRUE""",
        ),
        run(
            "fetchval",
            "SELECT COUNT(*)::int FROM products WHERE is_active = TRUE AND current_stock <= reorder_level",
        ),
        run("fetchval", """SELECT COUNT(*)::int FROM amc_contracts a WHERE a.status = 'ACTIVE' AND a.end_date <= CURRENT_DATE + INTERVAL '30 days'
               AND NOT EXISTS (SELECT 1 FROM amc_contracts r WHERE r.renewed_from_id = a.id AND r.status <> 'CANCELLED')"""),
        # Last 7 days (today included), zero-filled so the chart always has 7 bars.
        run(
            "fetch",
            """SELECT d.day::text AS date, COALESCE(SUM(i.total_amount),0) AS total, COUNT(i.id)::int AS count
               FROM (SELECT CURRENT_DATE - n AS day FROM generate_series(0, 6) n) d
               LEFT JOIN invoices i ON i.invoice_date = d.day AND i.status = 'ACTIVE'
               GROUP BY d.day ORDER BY d.day""",
        ),
        run(
            "fetch",
            """SELECT i.id, i.invoice_no, c.name AS customer_name, i.invoice_date::text AS invoice_date,
                      i.total_amount, i.paid_amount, i.payment_status
               FROM invoices i JOIN customers c ON c.id = i.customer_id
               WHERE i.status = 'ACTIVE'
               ORDER BY i.invoice_date DESC, i.created_at DESC LIMIT 5""",
        ),
        run(
            "fetch",
            """SELECT p.name, SUM(ii.quantity) AS qty_sold, SUM(ii.line_total) AS revenue
               FROM invoice_items ii
               JOIN invoices i ON i.id = ii.invoice_id
               JOIN products p ON p.id = ii.product_id
               WHERE i.status = 'ACTIVE' AND date_trunc('month', i.invoice_date) = date_trunc('month', CURRENT_DATE)
               GROUP BY p.name ORDER BY revenue DESC LIMIT 5""",
        ),
        run(
            "fetch",
            """SELECT id, name, current_stock, reorder_level FROM products
               WHERE is_active = TRUE AND current_stock <= reorder_level
               ORDER BY current_stock LIMIT 5""",
        ),
        run("fetchval", "SELECT COUNT(*)::int FROM invoices WHERE status = 'ACTIVE' AND payment_status <> 'PAID'"),
        # Service tickets: what is waiting on a technician, what is waiting on the owner, what is waiting to be billed.
        run(
            "fetchrow",
            """SELECT COUNT(*) FILTER (WHERE status IN ('OPEN', 'RESCHEDULED'))::int AS pending,
                      COUNT(*) FILTER (WHERE status IN ('OPEN', 'RESCHEDULED') AND scheduled_date < $1::date)::int AS overdue,
                      COUNT(*) FILTER (WHERE status = 'ATTENDED')::int AS awaiting_close,
                      COUNT(*) FILTER (WHERE service_type = 'PAID' AND status IN ('ATTENDED', 'CLOSED') AND invoice_id IS NULL)::int AS unbilled_paid,
                      COALESCE(SUM(charge_amount) FILTER (WHERE service_type = 'PAID' AND status IN ('ATTENDED', 'CLOSED') AND invoice_id IS NULL), 0) AS unbilled_amount
               FROM service_tickets""",
            today_local(),
        ),
        # The owner's to-do list: attended tickets, the ones waiting longest first.
        run(
            "fetch",
            """SELECT t.id, t.ticket_no, t.title, c.name AS customer_name, t.attended_at
               FROM service_tickets t JOIN customers c ON c.id = t.customer_id
               WHERE t.status = 'ATTENDED' ORDER BY t.attended_at, t.id LIMIT 5""",
        ),
    )

    return {
        "todaySales": row_to_dict(today_sales),
        "monthSales": row_to_dict(month_sales),
        "totalReceivables": float(receivables["total"]),
        "totalPayables": float(payables["total"]),
        "lowStockCount": low_stock_count,
        "amcExpiringCount": amc_due,
        # --- added for the redesigned dashboard (all additive; older clients ignore them) ---
        "previousMonthSales": row_to_dict(prev_month_sales),
        "salesTrend": rows_to_list(trend_rows),
        "recentInvoices": rows_to_list(recent_rows),
        "topProducts": rows_to_list(top_rows),
        "lowStockItems": rows_to_list(low_rows),
        "unpaidInvoiceCount": unpaid,
        # --- service tickets (v1.28.0) ---
        "serviceTickets": {
            "pending": ticket_stats["pending"],                    # OPEN or RESCHEDULED: waiting on a technician
            "overdue": ticket_stats["overdue"],                    # ...and their visit date has passed
            "awaitingClose": ticket_stats["awaiting_close"],       # ATTENDED: waiting on the owner
            "unbilledPaid": ticket_stats["unbilled_paid"],         # attended / closed PAID tickets not on an invoice
            "unbilledPaidAmount": float(ticket_stats["unbilled_amount"]),  # their estimated charges, ex-GST
        },
        "ticketsAwaitingClose": rows_to_list(ticket_review_rows),
    }


# ---------------------------------------------------------------------------------------------
# Service tickets report (v1.28.0)
# ---------------------------------------------------------------------------------------------
_TICKET_STATUSES = ("OPEN", "RESCHEDULED", "ATTENDED", "CLOSED", "CANCELLED")
_TICKET_TYPES = ("IN_WARRANTY", "PAID", "AMC")


def _local_day_bounds(start, end):
    """[start 00:00, day after end 00:00) in the store's time zone, so 'created on 5 March' means the shop's 5 March."""
    try:
        tz = ZoneInfo(settings.app_timezone)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.combine(start, time.min, tz), datetime.combine(end + timedelta(days=1), time.min, tz)


def _hours(value) -> float | None:
    return None if value is None else round(float(value), 1)


@router.get("/service-tickets")
async def service_tickets_report(from_: str = Query(default=None, alias="from"), to: str = None):
    """Tickets raised between `from` and `to` (inclusive): counts by status and type, per technician, and the average
    time to attend / close. `overdue` and `unbilled_paid` describe the situation right now, whatever the dates."""
    if not from_ or not to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to (YYYY-MM-DD) required")
    start, end = parse_date(from_), parse_date(to)
    if end < start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from must not be after to")
    if (end - start).days >= MAX_SALES_SUMMARY_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Date range too large (max 10 years)")
    lo, hi = _local_day_bounds(start, end)
    pool = get_pool()
    async with pool.acquire() as conn:
        counts = await conn.fetch(
            """SELECT status, service_type, COUNT(*)::int AS count FROM service_tickets
               WHERE created_at >= $1 AND created_at < $2 GROUP BY status, service_type""",
            lo, hi,
        )
        technicians = await conn.fetch(
            """SELECT u.id, u.name, COUNT(*)::int AS assigned,
                      COUNT(*) FILTER (WHERE t.attended_at IS NOT NULL)::int AS attended
               FROM service_tickets t JOIN users u ON u.id = t.assigned_to
               WHERE t.created_at >= $1 AND t.created_at < $2 AND t.status <> 'CANCELLED'
               GROUP BY u.id, u.name ORDER BY attended DESC, u.name, u.id""",
            lo, hi,
        )
        turnaround = await conn.fetchrow(
            """SELECT AVG(EXTRACT(EPOCH FROM (attended_at - created_at)) / 3600) AS to_attend,
                      AVG(EXTRACT(EPOCH FROM (closed_at - created_at)) / 3600) AS to_close
               FROM service_tickets WHERE created_at >= $1 AND created_at < $2""",
            lo, hi,
        )
        now = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status IN ('OPEN', 'RESCHEDULED') AND scheduled_date < $1::date)::int AS overdue,
                      COUNT(*) FILTER (WHERE service_type = 'PAID' AND status IN ('ATTENDED', 'CLOSED') AND invoice_id IS NULL)::int AS unbilled_count,
                      COALESCE(SUM(charge_amount) FILTER (WHERE service_type = 'PAID' AND status IN ('ATTENDED', 'CLOSED') AND invoice_id IS NULL), 0) AS unbilled_amount
               FROM service_tickets""",
            today_local(),
        )
    by_status = {k: 0 for k in _TICKET_STATUSES}
    by_type = {k: 0 for k in _TICKET_TYPES}
    for r in counts:
        by_status[r["status"]] += r["count"]
        by_type[r["service_type"]] += r["count"]
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "total": sum(by_status.values()),
        "by_status": by_status, "by_type": by_type,
        "by_technician": rows_to_list(technicians),
        "avg_hours_to_attend": _hours(turnaround["to_attend"]),
        "avg_hours_to_close": _hours(turnaround["to_close"]),
        "overdue": now["overdue"],
        "unbilled_paid": {"count": now["unbilled_count"], "amount": float(now["unbilled_amount"])},
    }


# ---------------------------------------------------------------------------------------------
# GSTR-1 style sales report, GSTR-3B style summary, CSV downloads (v1.19.0)
# ---------------------------------------------------------------------------------------------
def _period(from_: str | None, to: str | None):
    if not from_ or not to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to (YYYY-MM-DD) required")
    start, end = parse_date(from_), parse_date(to)
    if end < start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="to must not be before from")
    if (end - start).days > gstr.MAX_RANGE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Choose a period of at most {gstr.MAX_RANGE_DAYS} days (a month or a quarter is typical)")
    return start, end


async def _gstr1_data(start, end) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        store = await conn.fetchrow("SELECT gstin, state_code FROM store_profile WHERE id = 1")
        store_state = await get_store_state_code(conn)
        rate_rows = await conn.fetch(
            """SELECT i.invoice_no, i.invoice_date, i.total_amount, i.place_of_supply_state_code, i.is_interstate,
                      c.name AS customer_name, c.gstin AS customer_gstin, ii.gst_rate,
                      SUM(ROUND(ii.quantity * ii.unit_price, 2) - ii.discount_amount) AS taxable,
                      SUM(ii.cgst_amount) AS cgst, SUM(ii.sgst_amount) AS sgst, SUM(ii.igst_amount) AS igst
               FROM invoices i JOIN customers c ON c.id = i.customer_id JOIN invoice_items ii ON ii.invoice_id = i.id
               WHERE i.invoice_date BETWEEN $1 AND $2 AND i.status = 'ACTIVE'
               GROUP BY i.id, c.id, ii.gst_rate
               ORDER BY i.invoice_date, i.created_at, i.invoice_no, ii.gst_rate""",
            start, end,
        )
        hsn_rows = await conn.fetch(
            """SELECT COALESCE(NULLIF(ii.hsn_code, ''), NULLIF(p.hsn_code, ''), '') AS hsn_code,
                      COALESCE(p.unit, 'NA') AS unit, ii.gst_rate,
                      MIN(COALESCE(p.name, ii.description)) AS description,
                      SUM(ii.quantity) AS quantity,
                      SUM(ROUND(ii.quantity * ii.unit_price, 2) - ii.discount_amount) AS taxable,
                      SUM(ii.cgst_amount) AS cgst, SUM(ii.sgst_amount) AS sgst, SUM(ii.igst_amount) AS igst
               FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id LEFT JOIN products p ON p.id = ii.product_id
               WHERE i.invoice_date BETWEEN $1 AND $2 AND i.status = 'ACTIVE'
               GROUP BY 1, 2, ii.gst_rate ORDER BY 1, 2, ii.gst_rate""",
            start, end,
        )
        issued = await conn.fetch(
            """SELECT invoice_no, status FROM invoices WHERE invoice_date BETWEEN $1 AND $2
               ORDER BY created_at, invoice_no""",
            start, end,
        )
        cdn_rows = await conn.fetch(
            """SELECT n.credit_note_no, n.note_date, n.total_amount, n.place_of_supply_state_code, i.invoice_no, i.invoice_date,
                      c.name AS customer_name, c.gstin AS customer_gstin, ni.gst_rate,
                      SUM(ni.taxable_value) AS taxable, SUM(ni.cgst_amount) AS cgst, SUM(ni.sgst_amount) AS sgst,
                      SUM(ni.igst_amount) AS igst
               FROM credit_notes n JOIN credit_note_items ni ON ni.credit_note_id = n.id
               JOIN invoices i ON i.id = n.invoice_id JOIN customers c ON c.id = n.customer_id
               WHERE n.note_date BETWEEN $1 AND $2 AND n.status = 'ACTIVE'
               GROUP BY n.id, i.id, c.id, ni.gst_rate ORDER BY n.note_date, n.created_at, n.credit_note_no, ni.gst_rate""",
            start, end,
        )
        cn_issued = await conn.fetch(
            "SELECT credit_note_no, status FROM credit_notes WHERE note_date BETWEEN $1 AND $2 ORDER BY created_at, credit_note_no",
            start, end,
        )
        hsn_returns = await conn.fetch(
            """SELECT COALESCE(NULLIF(ni.hsn_code, ''), NULLIF(p.hsn_code, ''), '') AS hsn_code,
                      COALESCE(p.unit, 'NA') AS unit, ni.gst_rate,
                      SUM(ni.quantity) AS quantity, SUM(ni.taxable_value) AS taxable,
                      SUM(ni.cgst_amount) AS cgst, SUM(ni.sgst_amount) AS sgst, SUM(ni.igst_amount) AS igst
               FROM credit_note_items ni JOIN credit_notes n ON n.id = ni.credit_note_id LEFT JOIN products p ON p.id = ni.product_id
               WHERE n.note_date BETWEEN $1 AND $2 AND n.status = 'ACTIVE'
               GROUP BY 1, 2, ni.gst_rate""",
            start, end,
        )
    sections = gstr.build_sales_sections(rows_to_list(rate_rows), store_state)
    # Goods that came back are taken off the HSN summary (the portal wants it net of credit notes).
    sold = {(r["hsn_code"], r["unit"], r["gst_rate"]): dict(r) for r in rows_to_list(hsn_rows)}
    for r in rows_to_list(hsn_returns):
        key = (r["hsn_code"], r["unit"], r["gst_rate"])
        row = sold.setdefault(key, {**r, "description": None, "quantity": 0, "taxable": 0, "cgst": 0, "sgst": 0, "igst": 0})
        for k in ("quantity", "taxable", "cgst", "sgst", "igst"):
            row[k] = (row[k] or 0) - (r[k] or 0)
    hsn = gstr.build_hsn_rows(sorted(sold.values(), key=lambda r: (r["hsn_code"], r["unit"], r["gst_rate"])))
    docs = gstr.build_document_summary(rows_to_list(issued))
    cn_list = rows_to_list(cn_issued)
    docs.update(cn_from_no=cn_list[0]["credit_note_no"] if cn_list else None, cn_to_no=cn_list[-1]["credit_note_no"] if cn_list else None,
                cn_issued=len(cn_list), cn_cancelled=sum(1 for c in cn_list if c["status"] == "CANCELLED"))
    cdn = gstr.build_cdn_rows(rows_to_list(cdn_rows), store_state)
    sections["cdn"] = cdn
    totals = {k: round2(sum((r[k] for r in sections["rate_summary"]), 0)) for k in ("taxable", "cgst", "sgst", "igst")}
    totals["invoice_count"] = docs["net_issued"]
    credit_totals = {k: round2(sum((r[k] for r in cdn), 0)) for k in ("taxable", "cgst", "sgst", "igst")}
    credit_totals["note_count"] = docs["cn_issued"] - docs["cn_cancelled"]
    net_totals = {k: round2(totals[k] - credit_totals[k]) for k in ("taxable", "cgst", "sgst", "igst")}
    missing = [h for h in hsn if h["missing_hsn"]]
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "store_gstin": store["gstin"] if store else None, "store_state_code": store_state,
        "totals": totals, "credit_note_totals": credit_totals, "net_totals": net_totals, **sections, "hsn": hsn, "documents": docs,
        "warnings": {
            "lines_without_hsn": len(missing),
            "taxable_without_hsn": round2(sum((h["taxable"] for h in missing), 0)),
        },
        "notes": [
            "b2b / b2cl / b2cs show the invoices as issued. Credit notes of the period are listed in `cdn` and netted in `net_totals`; the HSN summary is already net of them.",
            f"B2CL = unregistered buyer, inter-state, invoice value above Rs. {gstr.B2CL_LIMIT:,.0f}.",
        ],
    }


@router.get("/gstr1")
async def gstr1(from_: str = Query(default=None, alias="from"), to: str = None):
    """Outward supplies grouped the way GSTR-1 asks for them: b2b, b2cl, b2cs, hsn, documents issued."""
    start, end = _period(from_, to)
    return await _gstr1_data(start, end)


_SECTIONS = ("all", "b2b", "b2cl", "b2cs", "cdn", "hsn", "docs")


@router.get("/gstr1/csv")
async def gstr1_csv(from_: str = Query(default=None, alias="from"), to: str = None, section: str = "all"):
    """The same figures as /reports/gstr1 as CSV: one section, or `section=all` for a zip of every section."""
    if section not in _SECTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"section must be one of {', '.join(_SECTIONS)}")
    start, end = _period(from_, to)
    data = await _gstr1_data(start, end)
    files = gstr.sales_csvs(data, data["hsn"], data["documents"])
    stem = f"gstr1-{start.isoformat()}-to-{end.isoformat()}"
    if section == "all":
        return Response(gstr.zip_files({f"{stem}-{name}": body for name, body in files.items()}),
                        media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.zip"'})
    return Response(files[f"{section}.csv"], media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{stem}-{section}.csv"'})


@router.get("/gstr3b")
async def gstr3b(from_: str = Query(default=None, alias="from"), to: str = None):
    """GSTR-3B style summary: tax on sales (3.1), input tax credit from purchases (4) and the difference per head.
    An estimate: ITC is set off head by head, without the cross-utilisation the GST portal allows. Credit notes (sales
    returns) issued in the period are already taken off the output tax, and debit notes (purchase returns) off the input credit."""
    start, end = _period(from_, to)
    sales = await gst_summary(start.isoformat(), end.isoformat())
    itc = await gst_input_credit(start.isoformat(), end.isoformat())
    out = {h: round2(sales[f"total_{h}"]) for h in ("igst", "cgst", "sgst")}
    credit = {h: round2(itc[f"total_{h}"]) for h in ("igst", "cgst", "sgst")}
    payable = {h: max(round2(out[h] - credit[h]), round2(0)) for h in out}
    carry = {h: max(round2(credit[h] - out[h]), round2(0)) for h in out}
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "outward_taxable_value": round2(sales["total_taxable"]), "output_tax": out,
        "inward_taxable_value": round2(itc["total_taxable"]), "input_tax_credit": credit,
        "net_payable": payable, "excess_credit": carry,
        "total_payable": round2(sum(payable.values(), round2(0))),
        "notes": ["Estimate only: check against the GST portal before paying; ITC cross-utilisation is not applied."],
    }


@router.get("/purchase-register/csv")
async def purchase_register_csv(from_: str = Query(default=None, alias="from"), to: str = None):
    """Purchases with the supplier's GSTIN and the tax split, for matching input tax credit against GSTR-2B."""
    start, end = _period(from_, to)
    pool = get_pool()
    async with pool.acquire() as conn:
        store_state = await get_store_state_code(conn)
        rows = await conn.fetch(
            """SELECT p.purchase_no, p.purchase_date, p.supplier_invoice_no, s.name AS supplier_name, s.gstin AS supplier_gstin,
                      s.state_code AS supplier_state_code, p.subtotal, p.total_gst, p.total_amount
               FROM purchases p JOIN suppliers s ON s.id = p.supplier_id
               WHERE p.purchase_date BETWEEN $1 AND $2 AND p.status = 'ACTIVE'
               ORDER BY p.purchase_date, p.created_at, p.purchase_no""",
            start, end,
        )
    body = []
    for r in rows:
        gst = r["total_gst"] or 0
        interstate = bool(r["supplier_state_code"] and r["supplier_state_code"] != store_state)
        cgst = round2(0) if interstate else round2(gst / 2)
        sgst = round2(0) if interstate else round2(gst) - cgst
        igst = round2(gst) if interstate else round2(0)
        body.append([r["supplier_gstin"], r["supplier_name"], r["supplier_invoice_no"], r["purchase_no"],
                     r["purchase_date"].strftime("%d-%m-%Y"), gstr.place_of_supply(r["supplier_state_code"]),
                     r["subtotal"], igst, cgst, sgst, r["total_amount"]])
    data = gstr.to_csv(["Supplier GSTIN", "Supplier", "Supplier Invoice No", "Our Purchase No", "Date", "Supplier State",
                        "Taxable Value", "IGST", "CGST", "SGST", "Total"], body)
    name = f"purchase-register-{start.isoformat()}-to-{end.isoformat()}.csv"
    return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})
