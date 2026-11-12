-- ============================================================
-- Migration 011: audit log (v1.10)
--
-- One append-only row per business action: who did it, when, what it was done to, and the
-- (secret-free) request that did it. Written by the API itself (see app/utils/audit.py);
-- read via GET /audit-logs (owner) and GET /admin/audit-logs (platform admin).
-- UPDATE and DELETE are blocked by a trigger, so history can't be edited from the API or by
-- accident. (TRUNCATE is not blocked, so a database restore still works.) Safe to re-run.
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      UUID REFERENCES users(id),  -- NULL for an attempt by someone who isn't a known account
    user_name    VARCHAR(120),               -- name / role as they were at the time
    user_role    VARCHAR(20),
    action       VARCHAR(40) NOT NULL,       -- CREATE, UPDATE, DELETE, CANCEL, PAYMENT, LOGIN, ...
    entity_type  VARCHAR(40) NOT NULL,       -- INVOICE, CUSTOMER, USER, ...
    entity_id    TEXT,
    summary      TEXT NOT NULL,              -- e.g. 'Cancelled invoice INV-0012'
    details      JSONB,                      -- sanitized request body etc. (never passwords / PINs)
    ip_address   VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id, created_at DESC);

CREATE OR REPLACE FUNCTION trg_audit_logs_append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs;
CREATE TRIGGER audit_logs_append_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION trg_audit_logs_append_only();
