-- v1.25.0: a profile image for every user.
-- The image itself lives in R2 (key users/profile/<random>.jpg); only its key is stored here. Safe to run more than once.
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_key VARCHAR(200);
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_updated_at TIMESTAMPTZ;
