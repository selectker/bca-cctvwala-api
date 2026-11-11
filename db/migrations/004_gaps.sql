-- ============================================================
-- Migration 004: session invalidation on password reset, quotation warranty months
-- Safe to re-run (idempotent).
-- ============================================================
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;

-- Customer warranty length chosen at quote time; carried onto the invoice when the
-- quotation is converted (barcodes themselves are supplied at conversion time).
ALTER TABLE quotation_items
    ADD COLUMN IF NOT EXISTS customer_warranty_months INTEGER;

CREATE INDEX IF NOT EXISTS idx_invoices_no ON invoices(invoice_no);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payments_purchase ON payments(purchase_id);
