"""AMC renewal link

Equivalent to db/migrations/023_amc_renewal.sql (idempotent; if applied by hand, `alembic stamp 0019`).

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20

"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE amc_contracts ADD COLUMN IF NOT EXISTS renewed_from_id UUID REFERENCES amc_contracts(id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_amc_renewed_from ON amc_contracts(renewed_from_id) WHERE renewed_from_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_amc_renewed_from")
    op.execute("ALTER TABLE amc_contracts DROP COLUMN IF EXISTS renewed_from_id")
