-- ============================================================
-- Migration 008: indexes for list/report queries that were still doing
-- full (or filtered-but-unindexed) scans — date-range filters, status
-- filters, and active+name listings used by the app's list endpoints.
-- Safe to run on an existing database.
-- ============================================================

-- ---------- Purchases: date-range filter + date-sorted listing (GET /purchases) ----------
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date);
CREATE INDEX IF NOT EXISTS idx_purchases_payment_status ON purchases(payment_status);

-- ---------- Invoices: payment_status filter (GET /invoices?payment_status=) ----------
CREATE INDEX IF NOT EXISTS idx_invoices_payment_status ON invoices(payment_status);

-- A customer's own invoice history (GET /customers/{id}) filters by
-- customer_id and sorts by invoice_date DESC in a single query — a
-- composite index serves that better than the two single-column ones.
CREATE INDEX IF NOT EXISTS idx_invoices_customer_date ON invoices(customer_id, invoice_date DESC);

-- ---------- Quotations: status filter + date-sorted listing (GET /quotations) ----------
CREATE INDEX IF NOT EXISTS idx_quotations_date ON quotations(quotation_date);
CREATE INDEX IF NOT EXISTS idx_quotations_status ON quotations(status);

-- ---------- AMC contracts: status filter combined with the "expiring within
-- N days" end_date range check (GET /amc?status=&expiring_within_days=) ----------
CREATE INDEX IF NOT EXISTS idx_amc_status_end_date ON amc_contracts(status, end_date);

-- ---------- Active-only listings sorted by name: customers, suppliers and
-- products list endpoints all filter is_active = TRUE then ORDER BY name ----------
CREATE INDEX IF NOT EXISTS idx_customers_active_name ON customers(name) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_suppliers_active_name ON suppliers(name) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_products_active_name ON products(name) WHERE is_active = TRUE;

-- ---------- Low-stock report (GET /reports/low-stock and the dashboard
-- counter): the predicate matches the query's WHERE clause exactly, so
-- Postgres can answer it with an index-only scan instead of scanning every
-- active product and comparing current_stock to reorder_level row by row. ----------
CREATE INDEX IF NOT EXISTS idx_products_low_stock_active
    ON products(current_stock)
    WHERE is_active = TRUE AND current_stock <= reorder_level;
