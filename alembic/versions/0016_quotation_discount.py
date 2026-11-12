"""Document-level discount on quotations

Equivalent to db/migrations/020_quotation_discount.sql (idempotent; if applied by hand, `alembic stamp 0016`).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20

"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS invoice_discount_percent NUMERIC(5,2) NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE quotations ADD COLUMN IF NOT EXISTS invoice_discount_amount NUMERIC(12,2) NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE quotations DROP COLUMN IF EXISTS invoice_discount_amount")
    op.execute("ALTER TABLE quotations DROP COLUMN IF EXISTS invoice_discount_percent")
