-- v1.13.2: sign-in failure counters live in their own small table instead of the append-only audit log.
--
-- Why: every failed sign-in used to be written to audit_logs, which a trigger makes impossible to prune, so anyone
-- could grow it without limit just by guessing, and the typed phone number (possibly a password typed into the
-- wrong box) was stored in it. login_failures holds only what the lock-outs need (a keyed hash of what was typed,
-- or the account id, plus the client address and time), is pruned automatically after 25 hours, and is neither
-- backed up nor restored. Safe to run more than once.

CREATE TABLE IF NOT EXISTS login_failures (
    subject     VARCHAR(80) NOT NULL,      -- account id, or a keyed hash of an unknown phone / account id
    ip_address  VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_login_failures_subject ON login_failures(subject, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_failures_created ON login_failures(created_at);
