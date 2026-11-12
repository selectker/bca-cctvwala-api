-- ============================================================
-- Migration 010: cancel invoices and purchases (v1.9)
--
-- A cancelled invoice / purchase is kept (its number is never reused) but flagged,
-- and left out of reports, balances and the dashboard. Cancelling reverses the stock
-- ledger and the unit warranties it created (see POST /invoices/{id}/cancel and
-- POST /purchases/{id}/cancel). Safe to re-run.
-- ============================================================

ALTER TABLE invoices ADD COLUMN IF NOT EXISTS status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CANCELLED'));
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cancel_reason TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cancelled_by UUID REFERENCES users(id);

ALTER TABLE purchases ADD COLUMN IF NOT EXISTS status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CANCELLED'));
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS cancel_reason TEXT;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS cancelled_by UUID REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_invoices_cancelled ON invoices(invoice_date) WHERE status = 'CANCELLED';
CREATE INDEX IF NOT EXISTS idx_purchases_cancelled ON purchases(purchase_date) WHERE status = 'CANCELLED';
