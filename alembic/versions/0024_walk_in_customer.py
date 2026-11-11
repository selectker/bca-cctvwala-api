"""Walk-in customer + buyer name on invoices

Equivalent to db/migrations/028_walk_in_customer.sql — kept in both places so a raw-psql deploy and an Alembic-managed
deploy stay in sync. If you've already applied that .sql file by hand, run `alembic stamp 0024` instead of
`alembic upgrade head`.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-08

"""
import re
from pathlib import Path

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "028_walk_in_customer.sql"


def upgrade() -> None:
    # The .sql file is idempotent and is the single source of truth. Comments are stripped first, then it is split on
    # ';' (there is no ';' inside any statement).
    text = "\n".join(re.sub(r"--.*$", "", line) for line in _SQL.read_text(encoding="utf-8").splitlines())
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_customers_single_walk_in")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS buyer_phone")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS buyer_name")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS is_walk_in")
