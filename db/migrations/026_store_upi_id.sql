-- v1.31.0: the shop's UPI ID (VPA). When set, an invoice that still has a balance prints a "scan to pay" QR code for that
-- balance, and GET /invoices/{id}/upi-link returns the same payment link. Safe to run more than once.
ALTER TABLE store_profile ADD COLUMN IF NOT EXISTS upi_id VARCHAR(100);
