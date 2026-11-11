-- v1.24.0: billing and shipping address on customers and invoices.
-- customers.address stays the billing address; customers.shipping_address is new (empty = same as billing).
-- Each invoice keeps its own copy of both addresses, so editing a customer later never changes an old invoice.
-- Existing invoices get the customer's current address as their billing address. Safe to run twice.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS shipping_address TEXT;
ALTER TABLE invoices  ADD COLUMN IF NOT EXISTS billing_address  TEXT;
ALTER TABLE invoices  ADD COLUMN IF NOT EXISTS shipping_address TEXT;
UPDATE invoices i SET billing_address = c.address
  FROM customers c WHERE c.id = i.customer_id AND i.billing_address IS NULL AND c.address IS NOT NULL;
