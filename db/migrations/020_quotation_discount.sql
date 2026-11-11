-- v1.22.0: document-level discount on quotations (same meaning as on invoices, see 019). Safe to run twice.
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS invoice_discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0;
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS invoice_discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0;
