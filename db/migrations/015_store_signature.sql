-- v1.16.0: the authorised signature, printed at the bottom of invoice PDFs. It is sent to the API as base64.
-- The image itself lives in R2 (key store/signature/<random>.png); only its key is stored here. Safe to run more than once.
ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS signature_key VARCHAR(200);
ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS signature_updated_at TIMESTAMPTZ;
