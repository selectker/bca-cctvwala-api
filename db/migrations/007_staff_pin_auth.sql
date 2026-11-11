-- ============================================================
-- Migration 007: split platform admin / owner bootstrap, and give
-- STAFF/TECHNICIAN accounts PIN-based login instead of a password.
--
-- Owner and platform admin still authenticate with phone + password
-- (stored in the existing password_hash column). Staff/technician
-- accounts have no self-signup and no password: the owner creates the
-- login and a one-time 6-digit PIN is generated, hashed into the same
-- password_hash column, and must be changed on first use.
--
-- Safe to re-run (idempotent).
-- ============================================================
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS must_change_pin BOOLEAN NOT NULL DEFAULT FALSE;

-- At most one OWNER can exist (mirrors the existing single-platform-admin rule).
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_single_owner ON users (role) WHERE role = 'OWNER';
