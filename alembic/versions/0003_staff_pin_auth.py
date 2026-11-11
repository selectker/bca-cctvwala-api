"""Staff/technician PIN login, single-owner constraint

Equivalent to db/migrations/007_staff_pin_auth.sql — kept in both places
so a raw-psql deploy and an Alembic-managed deploy stay in sync. If you've
already applied that .sql file by hand, run `alembic stamp 0003` instead of
`alembic upgrade head` to avoid re-running it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25

"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_pin BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_single_owner ON users (role) WHERE role = 'OWNER'")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_single_owner")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS must_change_pin")
