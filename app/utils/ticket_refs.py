"""Service tickets as they appear inside other screens (customer history, warranty unit). OWNER-only callers: the
charge and the invoice number are included."""
from app.utils.rows import rows_to_list

_COLUMNS = """t.id, t.ticket_no, t.title, t.service_type, t.status, t.scheduled_date, t.created_at, t.attended_at,
              t.closed_at, t.charge_amount, t.invoice_id, i.invoice_no, w.barcode AS warranty_barcode,
              t.amc_id, amc.contract_no AS amc_contract_no"""
_FROM = """FROM service_tickets t
           LEFT JOIN invoices i ON i.id = t.invoice_id
           LEFT JOIN part_warranties w ON w.id = t.warranty_id
           LEFT JOIN amc_contracts amc ON amc.id = t.amc_id"""

CUSTOMER_TICKET_LIMIT = 50


async def tickets_for_customer(conn, customer_id: str, limit: int = CUSTOMER_TICKET_LIMIT) -> tuple[list[dict], int]:
    """The customer's newest tickets (at most `limit`) and how many tickets the customer has in all."""
    rows = await conn.fetch(
        f"""SELECT {_COLUMNS}, COUNT(*) OVER() AS _total {_FROM}
            WHERE t.customer_id = $1 ORDER BY t.created_at DESC, t.id LIMIT $2""",
        customer_id, limit,
    )
    total = int(rows[0]["_total"]) if rows else 0
    return [{k: v for k, v in r.items() if k != "_total"} for r in rows_to_list(rows)], total


async def tickets_for_amc(conn, amc_id: str, limit: int = CUSTOMER_TICKET_LIMIT) -> tuple[list[dict], int]:
    """The newest tickets (at most `limit`) raised under this AMC contract, and how many there are in all."""
    rows = await conn.fetch(
        f"""SELECT {_COLUMNS}, COUNT(*) OVER() AS _total {_FROM}
            WHERE t.amc_id = $1::uuid ORDER BY t.created_at DESC, t.id LIMIT $2""",
        amc_id, limit,
    )
    total = int(rows[0]["_total"]) if rows else 0
    return [{k: v for k, v in r.items() if k != "_total"} for r in rows_to_list(rows)], total


async def tickets_for_warranty(conn, warranty_id) -> list[dict]:
    """Every ticket raised against this tracked unit, newest first."""
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} {_FROM} WHERE t.warranty_id = $1 ORDER BY t.created_at DESC, t.id", warranty_id,
    )
    return rows_to_list(rows)
