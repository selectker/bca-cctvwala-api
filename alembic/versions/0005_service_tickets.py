"""Service tickets (raise ticket -> attend / reschedule -> close -> bill)

Equivalent to db/migrations/009_service_tickets.sql — kept in both places
so a raw-psql deploy and an Alembic-managed deploy stay in sync. If you've
already applied that .sql file by hand, run `alembic stamp 0005` instead of
`alembic upgrade head` to avoid re-running it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13

"""
import re
from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "009_service_tickets.sql"


def upgrade() -> None:
    # The .sql file is idempotent (IF NOT EXISTS everywhere) and is the single source of truth.
    # Comments are stripped first, then it is split on ';' (there is no ';' inside any statement).
    text = "\n".join(re.sub(r"--.*$", "", line) for line in _SQL.read_text(encoding="utf-8").splitlines())
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    for idx in [
        "idx_service_ticket_images_ticket", "idx_service_ticket_events_ticket",
        "idx_service_tickets_no_trgm", "idx_service_tickets_invoice",
        "idx_service_tickets_assigned", "idx_service_tickets_status_created",
        "idx_service_tickets_customer",
    ]:
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS service_ticket_images")
    op.execute("DROP TABLE IF EXISTS service_ticket_events")
    op.execute("DROP TABLE IF EXISTS service_tickets")
