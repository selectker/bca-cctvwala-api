"""Document numbers: no duplicates under concurrency, and numeric (not text) ordering. Needs the test PostgreSQL."""
import asyncio

import asyncpg
import pytest

from app.utils.doc_number import current_fy, next_doc_number
from tests.helpers import raw_connect

TABLE = "doc_number_test"


async def _with_table(body):
    admin = await raw_connect()
    try:
        await admin.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await admin.execute(f"CREATE TABLE {TABLE} (no VARCHAR(30) UNIQUE NOT NULL)")
        await body(admin)
    finally:
        await admin.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await admin.close()


def test_concurrent_creates_get_distinct_consecutive_numbers():
    async def body(admin):
        conns = [await raw_connect() for _ in range(12)]

        async def create(conn):
            async with conn.transaction():
                number = await next_doc_number(conn, TABLE, "no", "T")
                await asyncio.sleep(0.05)  # widen the window in which a second creator would read the same "last" number
                await conn.execute(f"INSERT INTO {TABLE} (no) VALUES ($1)", number)  # unique violation if it ever repeats
            return number

        try:
            numbers = await asyncio.gather(*(create(c) for c in conns))
        finally:
            for c in conns:
                await c.close()
        stem = f"T/{current_fy()}/"
        assert sorted(numbers) == [f"{stem}{i:04d}" for i in range(1, 13)]

    asyncio.run(_with_table(body))


def test_numbers_are_compared_as_numbers_past_9999():
    async def body(admin):
        stem = f"T/{current_fy()}/"
        await admin.execute(f"INSERT INTO {TABLE} VALUES ($1), ($2)", stem + "9999", "T/1999-00/0500")  # other FY ignored

        async def nxt():
            async with admin.transaction():
                number = await next_doc_number(admin, TABLE, "no", "T")
                await admin.execute(f"INSERT INTO {TABLE} (no) VALUES ($1)", number)
            return number

        assert await nxt() == stem + "10000"   # text order would still say 9999 is the last one...
        assert await nxt() == stem + "10001"   # ...and propose 10000 again
        # a malformed tail doesn't confuse it
        await admin.execute(f"INSERT INTO {TABLE} VALUES ($1)", stem + "oops")
        assert await nxt() == stem + "10002"
        # '_' in a store-chosen prefix is a plain character, not a LIKE wildcard that matches "TX/..." rows
        await admin.execute(f"INSERT INTO {TABLE} VALUES ($1)", f"TX/{current_fy()}/0777")
        async with admin.transaction():
            assert await next_doc_number(admin, TABLE, "no", "T_") == f"T_/{current_fy()}/0001"

    asyncio.run(_with_table(body))


def test_it_refuses_to_run_outside_a_transaction():
    async def body(admin):
        with pytest.raises(RuntimeError, match="inside a transaction"):
            await next_doc_number(admin, TABLE, "no", "T")

    asyncio.run(_with_table(body))


def test_audit_log_cannot_be_truncated():
    async def go():
        conn = await raw_connect()
        try:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("TRUNCATE audit_logs")
        finally:
            await conn.close()

    asyncio.run(go())
