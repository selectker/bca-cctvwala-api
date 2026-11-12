-- v1.37.0: walk-in customer. One protected "Walk-in Customer" row (is_walk_in) that counter sales are billed to, and an
-- optional buyer name / phone on the invoice itself so the PDF can still show who actually bought. Idempotent.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_walk_in BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE invoices  ADD COLUMN IF NOT EXISTS buyer_name  VARCHAR(150);
ALTER TABLE invoices  ADD COLUMN IF NOT EXISTS buyer_phone VARCHAR(30);
-- at most one walk-in customer
CREATE UNIQUE INDEX IF NOT EXISTS uq_customers_single_walk_in ON customers (is_walk_in) WHERE is_walk_in;
-- the row itself is created (and kept on the shop's state) by the API the first time it is asked for: GET /customers/walk-in
