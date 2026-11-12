"""Index for the newest-first ticket list

Equivalent to db/migrations/027_ticket_list_index.sql — kept in both places so a raw-psql deploy and an Alembic-managed
deploy stay in sync. If you've already applied that .sql file by hand, run `alembic stamp 0023` instead of
`alembic upgrade head`.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-20

"""
import re
from pathlib import Path

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "027_ticket_list_index.sql"


def upgrade() -> None:
    # The .sql file is idempotent and is the single source of truth. Comments are stripped first, then it is split on
    # ';' (there is no ';' inside any statement).
    text = "\n".join(re.sub(r"--.*$", "", line) for line in _SQL.read_text(encoding="utf-8").splitlines())
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_service_tickets_created")
