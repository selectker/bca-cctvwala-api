-- v1.38.1: optional cost per unit (before GST) on a custom (no product) invoice line, so profit-loss is right for it. Idempotent.
ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS cost_price NUMERIC(12,2) CHECK (cost_price IS NULL OR cost_price >= 0);
