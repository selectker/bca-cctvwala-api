import asyncpg


def row_to_dict(row: asyncpg.Record | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in rows]
