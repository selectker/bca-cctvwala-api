import asyncpg

from app.utils.dates import financial_year_start, today_local

# First half of the two-int advisory lock key (the second half is a hash of the number series).
_DOC_NUMBER_LOCK_NAMESPACE = 727100


def current_fy() -> str:
    # The store's today (APP_TIMEZONE), the same day invoice dates are validated against.
    fy_start_year = financial_year_start(today_local()).year
    fy_end_year_short = str((fy_start_year + 1) % 100).zfill(2)
    return f"{fy_start_year}-{fy_end_year_short}"


async def next_doc_number(conn: asyncpg.Connection, table: str, column: str, prefix: str) -> str:
    """The next number of a series, e.g. INV/2026-27/0042.

    MUST be called inside a transaction, as the first thing it does: a transaction-level advisory lock on this series
    is taken here and released at commit / rollback, so two documents created at the same moment queue up instead of
    both proposing the same number (which used to fail on the unique constraint with a 500). Because every creator
    takes this lock before any row lock, no creator can hold a row another one needs while waiting for it.
    A rolled-back transaction gives its number back, so numbers stay consecutive.

    The sequence part is compared as a NUMBER (text order puts 9999 after 10000, which made the next number repeat
    after 9999 documents in a year). Rows of the series whose tail isn't a plain integer are ignored.
    """
    if not conn.is_in_transaction():
        raise RuntimeError("next_doc_number must be called inside a transaction (it relies on a transaction lock)")
    stem = f"{prefix}/{current_fy()}/"
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1::int, hashtext($2))", _DOC_NUMBER_LOCK_NAMESPACE, f"{table}:{stem}"
    )
    # table/column names are our own constants, never user input, so f-string is safe here.
    # starts_with() (not LIKE) so a '%' or '_' in a store-chosen prefix can't act as a wildcard.
    last = await conn.fetchval(
        f"""SELECT MAX(substr({column}, $2::int + 1)::bigint) FROM {table}
            WHERE starts_with({column}, $1) AND substr({column}, $2::int + 1) ~ '^[0-9]{{1,15}}$'""",
        stem, len(stem),
    )
    return f"{stem}{str((last or 0) + 1).zfill(4)}"


async def doc_prefix(conn: asyncpg.Connection, column: str, default: str) -> str:
    """Invoice / quotation prefix from the store profile (column is our own constant)."""
    value = await conn.fetchval(f"SELECT {column} FROM store_profile WHERE id = 1")
    return value or default
