"""Shop UPI ID for the pay-by-QR code on invoices

Equivalent to db/migrations/026_store_upi_id.sql — kept in both places so a raw-psql deploy and an Alembic-managed
deploy stay in sync. If you've already applied that .sql file by hand, run `alembic stamp 0022` instead of
`alembic upgrade head`.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-20

"""
import re
from pathlib import Path

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "026_store_upi_id.sql"


def upgrade() -> None:
    # The .sql file is idempotent and is the single source of truth. Comments are stripped first, then it is split on
    # ';' (there is no ';' inside any statement).
    text = "\n".join(re.sub(r"--.*$", "", line) for line in _SQL.read_text(encoding="utf-8").splitlines())
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS service_ticket_signoffs")
    op.execute("DROP INDEX IF EXISTS idx_service_tickets_amc")
    op.execute("ALTER TABLE service_tickets DROP CONSTRAINT IF EXISTS service_tickets_amc_link_check")
    op.execute("ALTER TABLE service_tickets DROP CONSTRAINT IF EXISTS service_tickets_service_type_check")
    op.execute("ALTER TABLE service_tickets DROP COLUMN IF EXISTS amc_id")
    op.execute(
        "ALTER TABLE service_tickets ADD CONSTRAINT service_tickets_service_type_check "
        "CHECK (service_type IN ('IN_WARRANTY', 'PAID'))"
    )
