"""Cost on custom invoice lines

Equivalent to db/migrations/030_custom_line_cost.sql — kept in both places so a raw-psql deploy and an Alembic-managed
deploy stay in sync. If you've already applied that .sql file by hand, run `alembic stamp 0026` instead of
`alembic upgrade head`.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-10

"""
import re
from pathlib import Path

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "030_custom_line_cost.sql"


def upgrade() -> None:
    # The .sql file is idempotent and is the single source of truth. Comments are stripped first, then it is split on
    # ';' (there is no ';' inside any statement).
    text = "\n".join(re.sub(r"--.*$", "", line) for line in _SQL.read_text(encoding="utf-8").splitlines())
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute("ALTER TABLE invoice_items DROP COLUMN IF EXISTS cost_price")
