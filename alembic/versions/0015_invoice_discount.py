"""Invoice-level discount

Equivalent to db/migrations/019_invoice_discount.sql (idempotent; if you applied that by hand, `alembic stamp 0015`).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-20

"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS invoice_discount_percent NUMERIC(5,2) NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS invoice_discount_amount NUMERIC(12,2) NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS invoice_discount_amount")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS invoice_discount_percent")
