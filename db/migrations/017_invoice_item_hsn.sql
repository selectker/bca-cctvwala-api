-- v1.19.0: the HSN / SAC code is now stored on every invoice line (copied from the product when the invoice is
-- made), so a later change to the product's HSN can't alter an old invoice or the GST reports. Safe to run twice.
-- Old lines stay NULL; reports and PDFs fall back to the product's current HSN for them.
ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS hsn_code VARCHAR(10);
