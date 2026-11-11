"""Server-side sessions + an audit log that survives a restore

Equivalent to db/migrations/012_sessions_and_audit_survives_restore.sql (read its header for the why). If you
applied that .sql file by hand, run `alembic stamp 0008` instead of `alembic upgrade head`; everything in it is
idempotent, so re-running is harmless anyway.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15

"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS auth_sessions (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at  TIMESTAMPTZ NOT NULL,
        revoked_at  TIMESTAMPTZ,
        ip_address  VARCHAR(64),
        user_agent  VARCHAR(200)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active ON auth_sessions(user_id) WHERE revoked_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)",
    """DO $$
    DECLARE c record;
    BEGIN
        FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'audit_logs'::regclass AND contype = 'f'
        LOOP
            EXECUTE format('ALTER TABLE audit_logs DROP CONSTRAINT %I', c.conname);
        END LOOP;
    END $$""",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_login_failed ON audit_logs ((details->>'identifier'), created_at DESC) WHERE action = 'LOGIN_FAILED'",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_login_ok ON audit_logs (user_id, ip_address, created_at DESC) WHERE action = 'LOGIN'",
    """CREATE OR REPLACE FUNCTION trg_audit_logs_no_truncate() RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'audit_logs cannot be truncated';
    END;
    $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs",
    """CREATE TRIGGER audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION trg_audit_logs_no_truncate()""",
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS trg_audit_logs_no_truncate()")
    op.execute("DROP INDEX IF EXISTS idx_audit_logs_login_ok")
    op.execute("DROP INDEX IF EXISTS idx_audit_logs_login_failed")
    # The audit_logs -> users foreign key is not put back: rows may now name users a restore removed.
    op.execute("DROP TABLE IF EXISTS auth_sessions")
