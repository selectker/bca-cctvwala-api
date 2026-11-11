"""Authorised signature for invoice PDFs

Equivalent to db/migrations/015_store_signature.sql. If you applied that .sql file by hand, run `alembic stamp 0011`
instead of `alembic upgrade head`; it is idempotent, so re-running is harmless anyway.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15

"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS signature_key VARCHAR(200)")
    op.execute("ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS signature_updated_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE store_profile DROP COLUMN IF EXISTS signature_updated_at")
    op.execute("ALTER TABLE store_profile DROP COLUMN IF EXISTS signature_key")
