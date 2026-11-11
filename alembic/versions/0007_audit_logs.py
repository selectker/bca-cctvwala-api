"""Audit log

Equivalent to db/migrations/011_audit_logs.sql — kept in both places so a raw-psql deploy and an
Alembic-managed deploy stay in sync. If you've already applied that .sql file by hand, run
`alembic stamp 0007` instead of `alembic upgrade head` to avoid re-running it. (Everything in it is
idempotent, so re-running is harmless anyway.)

The statements are written out here instead of parsed from the .sql file, because the trigger
function contains a ';' inside its $$ body, which the splitter used by 0005 / 0006 can't handle.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14

"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS audit_logs (
        id           BIGSERIAL PRIMARY KEY,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        user_id      UUID REFERENCES users(id),
        user_name    VARCHAR(120),
        user_role    VARCHAR(20),
        action       VARCHAR(40) NOT NULL,
        entity_type  VARCHAR(40) NOT NULL,
        entity_id    TEXT,
        summary      TEXT NOT NULL,
        details      JSONB,
        ip_address   VARCHAR(64)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id, created_at DESC)",
    """CREATE OR REPLACE FUNCTION trg_audit_logs_append_only() RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'audit_logs is append-only';
    END;
    $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs",
    """CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION trg_audit_logs_append_only()""",
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS trg_audit_logs_append_only()")
    for idx in ("idx_audit_logs_user", "idx_audit_logs_entity", "idx_audit_logs_created"):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS audit_logs")
