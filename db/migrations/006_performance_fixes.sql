-- ============================================================
-- Performance fixes: missing FK indexes, maintained stock column,
-- and trigram indexes for ILIKE '%...%' searches.
-- Safe to run on an existing database.
-- ============================================================

-- ---------- 1. Missing FK indexes (sequential scans on hot paths) ----------
CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_quotation_items_quotation ON quotation_items(quotation_id);
CREATE INDEX IF NOT EXISTS idx_purchase_items_purchase ON purchase_items(purchase_id);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payments_purchase ON payments(purchase_id);
CREATE INDEX IF NOT EXISTS idx_part_warranties_invoice_item ON part_warranties(invoice_item_id);
CREATE INDEX IF NOT EXISTS idx_part_warranties_purchase_item ON part_warranties(purchase_item_id);
CREATE INDEX IF NOT EXISTS idx_part_warranties_purchase ON part_warranties(purchase_id);
CREATE INDEX IF NOT EXISTS idx_part_warranties_invoice ON part_warranties(invoice_id);
CREATE INDEX IF NOT EXISTS idx_amc_service_visits_contract ON amc_service_visits(contract_id);

-- ---------- 2. Maintained current_stock column (replaces live SUM(stock_ledger) on every read) ----------
ALTER TABLE products ADD COLUMN IF NOT EXISTS current_stock NUMERIC(12,2) NOT NULL DEFAULT 0;

-- Backfill from existing ledger history
UPDATE products p SET current_stock = COALESCE(
    (SELECT SUM(sl.quantity) FROM stock_ledger sl WHERE sl.product_id = p.id), 0
);

-- Keep it in sync going forward. stock_ledger is append-only in normal app flow,
-- but the trigger also covers UPDATE/DELETE defensively (e.g. manual fixes, restores).
CREATE OR REPLACE FUNCTION trg_update_product_stock() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE products SET current_stock = current_stock + NEW.quantity WHERE id = NEW.product_id;
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        UPDATE products SET current_stock = current_stock - OLD.quantity + NEW.quantity WHERE id = NEW.product_id;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE products SET current_stock = current_stock - OLD.quantity WHERE id = OLD.product_id;
        RETURN OLD;
    END IF;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS stock_ledger_maintain_product_stock ON stock_ledger;
CREATE TRIGGER stock_ledger_maintain_product_stock
    AFTER INSERT OR UPDATE OR DELETE ON stock_ledger
    FOR EACH ROW EXECUTE FUNCTION trg_update_product_stock();

CREATE INDEX IF NOT EXISTS idx_products_current_stock ON products(current_stock);

-- ---------- 3. Trigram indexes for leading-wildcard ILIKE '%...%' searches ----------
-- A plain btree index can't be used when the search has a leading '%'; these let
-- Postgres use an index instead of a full table scan as tables grow.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_invoices_no_trgm ON invoices USING gin (invoice_no gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_purchases_no_trgm ON purchases USING gin (purchase_no gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_purchases_supplier_invoice_no_trgm ON purchases USING gin (supplier_invoice_no gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_quotations_no_trgm ON quotations USING gin (quotation_no gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_amc_contract_no_trgm ON amc_contracts USING gin (contract_no gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_customers_name_trgm ON customers USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_customers_phone_trgm ON customers USING gin (phone gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_suppliers_name_trgm ON suppliers USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_suppliers_phone_trgm ON suppliers USING gin (phone gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_name_trgm ON products USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_sku_trgm ON products USING gin (sku gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_part_warranties_barcode_trgm ON part_warranties USING gin (barcode gin_trgm_ops);
