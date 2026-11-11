"""Store logo for invoice / quotation PDFs

Equivalent to db/migrations/014_store_logo.sql. If you applied that .sql file by hand, run `alembic stamp 0010`
instead of `alembic upgrade head`; it is idempotent, so re-running is harmless anyway.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-15

"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS logo_key VARCHAR(200)")
    op.execute("ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS logo_updated_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE store_profile DROP COLUMN IF EXISTS logo_updated_at")
    op.execute("ALTER TABLE store_profile DROP COLUMN IF EXISTS logo_key")
