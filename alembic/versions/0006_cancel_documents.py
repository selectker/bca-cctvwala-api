"""Cancel invoices and purchases

Equivalent to db/migrations/010_cancel_documents.sql — kept in both places
so a raw-psql deploy and an Alembic-managed deploy stay in sync. If you've
already applied that .sql file by hand, run `alembic stamp 0006` instead of
`alembic upgrade head` to avoid re-running it.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14

"""
import re
from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "010_cancel_documents.sql"


def upgrade() -> None:
    # The .sql file is idempotent (IF NOT EXISTS everywhere) and is the single source of truth.
    text = "\n".join(re.sub(r"--.*$", "", line) for line in _SQL.read_text(encoding="utf-8").splitlines())
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_purchases_cancelled")
    op.execute("DROP INDEX IF EXISTS idx_invoices_cancelled")
    for table in ("purchases", "invoices"):
        for col in ("cancelled_by", "cancel_reason", "cancelled_at", "status"):
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}")
