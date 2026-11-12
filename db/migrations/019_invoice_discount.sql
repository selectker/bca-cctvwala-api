-- v1.21.0: invoice-level discount. The discount is spread over the invoice lines before GST and added to each
-- line's discount_amount (so taxable values, GST and GSTR-1 stay correct); these two columns just remember what
-- was entered, for display. invoice_discount_percent is 0 when a flat amount was given. Safe to run twice.
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS invoice_discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS invoice_discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0;
