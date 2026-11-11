-- v1.15.0: the shop's logo, printed in the header of invoice and quotation PDFs.
-- The image itself lives in R2 (key store/logo/<random>.png); only its key is stored here. Safe to run more than once.
ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS logo_key VARCHAR(200);
ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS logo_updated_at TIMESTAMPTZ;
