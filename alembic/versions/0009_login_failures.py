"""Sign-in failure counters in their own prunable table

Equivalent to db/migrations/013_login_failures.sql (read its header for the why). If you applied that .sql file by
hand, run `alembic stamp 0009` instead of `alembic upgrade head`; it is idempotent, so re-running is harmless anyway.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15

"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS login_failures (
        subject     VARCHAR(80) NOT NULL,
        ip_address  VARCHAR(64),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_login_failures_subject ON login_failures(subject, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_login_failures_created ON login_failures(created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS login_failures")
