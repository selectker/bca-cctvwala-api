-- ============================================================
-- Migration 012 (v1.12): server-side sessions + an audit log that survives a restore
--
-- 1. auth_sessions: every sign-in is a row; the token is only good while its row is active, so logout,
--    "sign out everywhere", an owner ending a staff member's sessions, and password / PIN changes really end
--    access (before, a stolen token worked for its full 30 days). It has NO foreign key to users on purpose:
--    a restore truncates users, and sessions are deliberately neither backed up nor restored. A session whose
--    user no longer exists simply stops working.
--    Tokens issued before this migration carry no session id and are refused: everyone signs in once more.
--
-- 2. audit_logs no longer references users, so a restore can replace the users table without touching the
--    log (the row keeps the user's name / role as they were anyway). TRUNCATE on it is now refused by a trigger
--    like UPDATE / DELETE already were, and the restore code no longer truncates it. Two partial indexes
--    serve the per-address sign-in lock-out.
--
-- Safe to re-run.
-- ============================================================

CREATE TABLE IF NOT EXISTS auth_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    ip_address  VARCHAR(64),
    user_agent  VARCHAR(200)
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active ON auth_sessions(user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at);

DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'audit_logs'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE audit_logs DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_audit_logs_login_failed ON audit_logs ((details->>'identifier'), created_at DESC) WHERE action = 'LOGIN_FAILED';
CREATE INDEX IF NOT EXISTS idx_audit_logs_login_ok ON audit_logs (user_id, ip_address, created_at DESC) WHERE action = 'LOGIN';

CREATE OR REPLACE FUNCTION trg_audit_logs_no_truncate() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs cannot be truncated';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs;
CREATE TRIGGER audit_logs_no_truncate
    BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION trg_audit_logs_no_truncate();
