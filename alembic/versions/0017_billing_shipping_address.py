"""Billing and shipping address on customers and invoices

Equivalent to db/migrations/021_billing_shipping_address.sql (idempotent; if applied by hand, `alembic stamp 0017`).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-20

"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS shipping_address TEXT")
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billing_address TEXT")
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS shipping_address TEXT")
    op.execute(
        "UPDATE invoices i SET billing_address = c.address FROM customers c "
        "WHERE c.id = i.customer_id AND i.billing_address IS NULL AND c.address IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS shipping_address")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS billing_address")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS shipping_address")
