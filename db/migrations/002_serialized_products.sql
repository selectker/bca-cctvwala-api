-- ============================================================
-- Migration: serialized-product flag
-- Marks which products are tracked unit-by-unit (barcode/serial),
-- e.g. cameras/DVRs, vs bulk items (cable, connectors) that aren't.
-- Purchases/invoices use this to know when to expect barcodes so
-- purchase -> warranty -> sale stay linked automatically.
-- Safe to re-run (idempotent).
-- ============================================================

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS is_serialized BOOLEAN NOT NULL DEFAULT FALSE;
