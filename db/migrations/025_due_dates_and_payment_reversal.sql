-- v1.30.0: (1) payment terms, due dates and a credit limit, (2) payments can be reversed (not deleted).
-- Safe to run more than once.

-- 1. A customer can have default payment terms (days) and a credit limit (null = no limit). Each invoice keeps its own
--    due date; null means "due on the invoice date" everywhere (overdue lists, ageing).
ALTER TABLE customers ADD COLUMN IF NOT EXISTS credit_limit NUMERIC(12,2) CHECK (credit_limit IS NULL OR credit_limit >= 0);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS payment_terms_days INTEGER CHECK (payment_terms_days IS NULL OR payment_terms_days BETWEEN 0 AND 365);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS due_date DATE;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_terms_days INTEGER CHECK (payment_terms_days IS NULL OR payment_terms_days BETWEEN 0 AND 365);
CREATE INDEX IF NOT EXISTS idx_invoices_unpaid_due ON invoices (COALESCE(due_date, invoice_date)) WHERE status = 'ACTIVE' AND payment_status <> 'PAID';

-- 2. A wrongly entered payment is never edited or deleted (the money trail stays intact): it is reversed. The reversal
--    is a second, negative payment row that points back at the original, and the original is marked reversed.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reversed_by UUID REFERENCES users(id);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reversal_reason VARCHAR(500);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reverses_payment_id UUID REFERENCES payments(id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_reverses ON payments (reverses_payment_id) WHERE reverses_payment_id IS NOT NULL;
