"""Indexes for hot query paths: date-range filters, status filters, active+name listings, low-stock report

Equivalent to db/migrations/008_query_indexes.sql — kept in both places
so a raw-psql deploy and an Alembic-managed deploy stay in sync. If you've
already applied that .sql file by hand, run `alembic stamp 0004` instead of
`alembic upgrade head` to avoid re-running it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25

"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- Purchases: date-range filter + date-sorted listing ----------
    op.execute("CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_purchases_payment_status ON purchases(payment_status)")

    # ---------- Invoices: payment_status filter + per-customer date-sorted history ----------
    op.execute("CREATE INDEX IF NOT EXISTS idx_invoices_payment_status ON invoices(payment_status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoices_customer_date ON invoices(customer_id, invoice_date DESC)"
    )

    # ---------- Quotations: status filter + date-sorted listing ----------
    op.execute("CREATE INDEX IF NOT EXISTS idx_quotations_date ON quotations(quotation_date)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_quotations_status ON quotations(status)")

    # ---------- AMC contracts: status + expiring-within-N-days end_date range ----------
    op.execute("CREATE INDEX IF NOT EXISTS idx_amc_status_end_date ON amc_contracts(status, end_date)")

    # ---------- Active-only listings sorted by name ----------
    op.execute("CREATE INDEX IF NOT EXISTS idx_customers_active_name ON customers(name) WHERE is_active = TRUE")
    op.execute("CREATE INDEX IF NOT EXISTS idx_suppliers_active_name ON suppliers(name) WHERE is_active = TRUE")
    op.execute("CREATE INDEX IF NOT EXISTS idx_products_active_name ON products(name) WHERE is_active = TRUE")

    # ---------- Low-stock report: predicate matches the query exactly ----------
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_products_low_stock_active
            ON products(current_stock)
            WHERE is_active = TRUE AND current_stock <= reorder_level
        """
    )


def downgrade() -> None:
    for idx in [
        "idx_purchases_date", "idx_purchases_payment_status",
        "idx_invoices_payment_status", "idx_invoices_customer_date",
        "idx_quotations_date", "idx_quotations_status",
        "idx_amc_status_end_date",
        "idx_customers_active_name", "idx_suppliers_active_name", "idx_products_active_name",
        "idx_products_low_stock_active",
    ]:
        op.execute(f"DROP INDEX IF EXISTS {idx}")
