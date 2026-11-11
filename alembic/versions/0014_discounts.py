"""Per-line discount on quotations and invoices

Equivalent to db/migrations/018_discounts.sql. If you applied that .sql file by hand, run `alembic stamp 0014`
instead of `alembic upgrade head`; it is idempotent, so re-running is harmless anyway.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19

"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("quotation_items", "discount_percent", "NUMERIC(5,2)"),
    ("quotation_items", "discount_amount", "NUMERIC(12,2)"),
    ("invoice_items", "discount_percent", "NUMERIC(5,2)"),
    ("invoice_items", "discount_amount", "NUMERIC(12,2)"),
    ("quotations", "total_discount", "NUMERIC(12,2)"),
    ("invoices", "total_discount", "NUMERIC(12,2)"),
]


def upgrade() -> None:
    for table, column, kind in _COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind} NOT NULL DEFAULT 0")


def downgrade() -> None:
    for table, column, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
