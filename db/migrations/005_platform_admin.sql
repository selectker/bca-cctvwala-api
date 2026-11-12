-- ============================================================
-- Migration 005: platform admin (the first account ever registered).
-- Only the platform admin may back up / restore the whole database.
-- Safe to re-run (idempotent).
-- ============================================================
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- At most one platform admin can exist.
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_single_platform_admin
    ON users ((TRUE)) WHERE is_platform_admin;

-- Existing installs: the earliest-created account becomes the platform admin.
UPDATE users SET is_platform_admin = TRUE
WHERE id = (SELECT id FROM users ORDER BY created_at, id LIMIT 1)
  AND NOT EXISTS (SELECT 1 FROM users WHERE is_platform_admin);
