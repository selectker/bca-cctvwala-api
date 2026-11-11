"""Business expenses

Equivalent to db/migrations/016_expenses.sql. If you applied that .sql file by hand, run `alembic stamp 0012`
instead of `alembic upgrade head`; it is idempotent, so re-running is harmless anyway.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20

"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS expenses (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            expense_date  DATE NOT NULL DEFAULT CURRENT_DATE,
            category      VARCHAR(60) NOT NULL,
            amount        NUMERIC(12,2) NOT NULL CHECK (amount > 0),
            payment_mode  VARCHAR(15) NOT NULL DEFAULT 'CASH',
            paid_to       VARCHAR(120),
            reference_no  VARCHAR(50),
            notes         TEXT,
            created_by    UUID REFERENCES users(id),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ
        )"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (expense_date DESC, id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses (lower(category), expense_date DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS expenses")
