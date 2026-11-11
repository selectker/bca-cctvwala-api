"""Performance fixes: missing FK indexes, maintained products.current_stock, trigram search indexes

Equivalent to db/migrations/006_performance_fixes.sql — kept in both places
so a raw-psql deploy and an Alembic-managed deploy stay in sync. If you've
already applied that .sql file by hand, run `alembic stamp 0002` instead of
`alembic upgrade head` to avoid re-running it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25

"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- Missing FK indexes ----------
    op.execute("CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(invoice_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_quotation_items_quotation ON quotation_items(quotation_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_purchase_items_purchase ON purchase_items(purchase_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_payments_purchase ON payments(purchase_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_part_warranties_invoice_item ON part_warranties(invoice_item_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_part_warranties_purchase_item ON part_warranties(purchase_item_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_part_warranties_purchase ON part_warranties(purchase_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_part_warranties_invoice ON part_warranties(invoice_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_amc_service_visits_contract ON amc_service_visits(contract_id)")

    # ---------- Maintained current_stock column ----------
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS current_stock NUMERIC(12,2) NOT NULL DEFAULT 0")
    op.execute(
        """
        UPDATE products p SET current_stock = COALESCE(
            (SELECT SUM(sl.quantity) FROM stock_ledger sl WHERE sl.product_id = p.id), 0
        )
        """
    )
    op.execute(
        """
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
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS stock_ledger_maintain_product_stock ON stock_ledger")
    op.execute(
        """
        CREATE TRIGGER stock_ledger_maintain_product_stock
            AFTER INSERT OR UPDATE OR DELETE ON stock_ledger
            FOR EACH ROW EXECUTE FUNCTION trg_update_product_stock()
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_products_current_stock ON products(current_stock)")

    # ---------- Trigram search indexes ----------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX IF NOT EXISTS idx_invoices_no_trgm ON invoices USING gin (invoice_no gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_purchases_no_trgm ON purchases USING gin (purchase_no gin_trgm_ops)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_purchases_supplier_invoice_no_trgm "
        "ON purchases USING gin (supplier_invoice_no gin_trgm_ops)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_quotations_no_trgm ON quotations USING gin (quotation_no gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_amc_contract_no_trgm ON amc_contracts USING gin (contract_no gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_customers_name_trgm ON customers USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_customers_phone_trgm ON customers USING gin (phone gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_suppliers_name_trgm ON suppliers USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_suppliers_phone_trgm ON suppliers USING gin (phone gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_products_name_trgm ON products USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_products_sku_trgm ON products USING gin (sku gin_trgm_ops)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_part_warranties_barcode_trgm "
        "ON part_warranties USING gin (barcode gin_trgm_ops)"
    )


def downgrade() -> None:
    # Trigram indexes (leave the pg_trgm extension itself installed — harmless, and
    # something else may come to depend on it)
    for idx in [
        "idx_invoices_no_trgm", "idx_purchases_no_trgm", "idx_purchases_supplier_invoice_no_trgm",
        "idx_quotations_no_trgm", "idx_amc_contract_no_trgm", "idx_customers_name_trgm",
        "idx_customers_phone_trgm", "idx_suppliers_name_trgm", "idx_suppliers_phone_trgm",
        "idx_products_name_trgm", "idx_products_sku_trgm", "idx_part_warranties_barcode_trgm",
    ]:
        op.execute(f"DROP INDEX IF EXISTS {idx}")

    # Maintained stock column
    op.execute("DROP TRIGGER IF EXISTS stock_ledger_maintain_product_stock ON stock_ledger")
    op.execute("DROP FUNCTION IF EXISTS trg_update_product_stock()")
    op.execute("DROP INDEX IF EXISTS idx_products_current_stock")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS current_stock")

    # FK indexes
    for idx in [
        "idx_invoice_items_invoice", "idx_quotation_items_quotation", "idx_purchase_items_purchase",
        "idx_payments_invoice", "idx_payments_purchase", "idx_part_warranties_invoice_item",
        "idx_part_warranties_purchase_item", "idx_part_warranties_purchase", "idx_part_warranties_invoice",
        "idx_amc_service_visits_contract",
    ]:
        op.execute(f"DROP INDEX IF EXISTS {idx}")
