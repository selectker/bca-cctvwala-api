-- v1.20.0: per-line trade discount on quotations and invoices. discount_percent is what the user typed (0-100),
-- discount_amount is the rupee value taken off quantity x unit_price before GST, total_discount is the sum on the
-- document. `subtotal` stays the taxable value (i.e. already after discount), so GST reports need no change for
-- old rows: they default to 0. Safe to run twice.
ALTER TABLE quotation_items ADD COLUMN IF NOT EXISTS discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0;
ALTER TABLE quotation_items ADD COLUMN IF NOT EXISTS discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE invoice_items   ADD COLUMN IF NOT EXISTS discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0;
ALTER TABLE invoice_items   ADD COLUMN IF NOT EXISTS discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE quotations      ADD COLUMN IF NOT EXISTS total_discount   NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE invoices        ADD COLUMN IF NOT EXISTS total_discount   NUMERIC(12,2) NOT NULL DEFAULT 0;
