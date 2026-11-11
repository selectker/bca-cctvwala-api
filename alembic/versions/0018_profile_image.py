"""Profile image for users

Equivalent to db/migrations/022_profile_image.sql (idempotent; if applied by hand, `alembic stamp 0018`).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-20

"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_key VARCHAR(200)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_updated_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS avatar_updated_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS avatar_key")
