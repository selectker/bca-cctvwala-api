"""HSN / SAC code stored on invoice lines

Equivalent to db/migrations/017_invoice_item_hsn.sql. If you applied that .sql file by hand, run `alembic stamp 0013`
instead of `alembic upgrade head`; it is idempotent, so re-running is harmless anyway.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19

"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS hsn_code VARCHAR(10)")


def downgrade() -> None:
    op.execute("ALTER TABLE invoice_items DROP COLUMN IF EXISTS hsn_code")
