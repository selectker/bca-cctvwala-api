"""Credit notes, debit notes, purchase attachment

Equivalent to db/migrations/029_credit_debit_notes.sql — kept in both places so a raw-psql deploy and an Alembic-managed
deploy stay in sync. If you've already applied that .sql file by hand, run `alembic stamp 0025` instead of
`alembic upgrade head`.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-10

"""
import re
from pathlib import Path

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "029_credit_debit_notes.sql"


def upgrade() -> None:
    # The .sql file is idempotent and is the single source of truth. Comments are stripped first, then it is split on
    # ';' (there is no ';' inside any statement).
    text = "\n".join(re.sub(r"--.*$", "", line) for line in _SQL.read_text(encoding="utf-8").splitlines())
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    for stmt in (
        "ALTER TABLE purchases DROP COLUMN IF EXISTS attachment_name",
        "ALTER TABLE purchases DROP COLUMN IF EXISTS attachment_type",
        "ALTER TABLE purchases DROP COLUMN IF EXISTS attachment_key",
        "ALTER TABLE payments DROP COLUMN IF EXISTS debit_note_id",
        "ALTER TABLE payments DROP COLUMN IF EXISTS credit_note_id",
        "DROP TABLE IF EXISTS debit_note_items",
        "DROP TABLE IF EXISTS debit_notes",
        "DROP TABLE IF EXISTS credit_note_items",
        "DROP TABLE IF EXISTS credit_notes",
    ):
        op.execute(stmt)
