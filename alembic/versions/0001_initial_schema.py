"""Initial schema (baseline)

This mirrors schema.sql as originally shipped, before the performance-fix
migration (0002). If your database was already created by running
schema.sql directly, don't run this one — instead run:

    alembic stamp 0001

to tell Alembic "this revision is already applied" without re-running it,
then `alembic upgrade head` will apply only 0002 onward.

For a brand-new database, `alembic upgrade head` runs this one too.

Revision ID: 0001
Revises:
Create Date: 2026-09-25

"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS "pgcrypto";

        CREATE TABLE users (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name            VARCHAR(120) NOT NULL,
            phone           VARCHAR(15) UNIQUE NOT NULL,
            email           VARCHAR(150) UNIQUE,
            password_hash   TEXT NOT NULL,
            role            VARCHAR(20) NOT NULL DEFAULT 'STAFF',
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            password_changed_at TIMESTAMPTZ,
            is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX uq_users_single_platform_admin ON users ((TRUE)) WHERE is_platform_admin;

        CREATE TABLE store_profile (
            id              SMALLINT PRIMARY KEY DEFAULT 1,
            store_name      VARCHAR(150) NOT NULL,
            gstin           VARCHAR(15),
            address         TEXT,
            state           VARCHAR(50),
            state_code      VARCHAR(2),
            phone           VARCHAR(15),
            email           VARCHAR(150),
            bank_name       VARCHAR(150),
            bank_account_no VARCHAR(30),
            bank_ifsc       VARCHAR(15),
            invoice_prefix  VARCHAR(10) DEFAULT 'INV',
            quotation_prefix VARCHAR(10) DEFAULT 'QUO',
            CONSTRAINT single_row CHECK (id = 1)
        );

        CREATE TABLE suppliers (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name            VARCHAR(150) NOT NULL,
            gstin           VARCHAR(15),
            phone           VARCHAR(15),
            email           VARCHAR(150),
            address         TEXT,
            state           VARCHAR(50),
            state_code      VARCHAR(2),
            opening_balance NUMERIC(12,2) DEFAULT 0,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE customers (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name            VARCHAR(150) NOT NULL,
            gstin           VARCHAR(15),
            phone           VARCHAR(15),
            email           VARCHAR(150),
            address         TEXT,
            state           VARCHAR(50),
            state_code      VARCHAR(2),
            customer_type   VARCHAR(10) DEFAULT 'B2C',
            opening_balance NUMERIC(12,2) DEFAULT 0,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE categories (
            id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name    VARCHAR(100) NOT NULL UNIQUE
        );

        CREATE TABLE products (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            sku             VARCHAR(50) UNIQUE,
            name            VARCHAR(200) NOT NULL,
            category_id     UUID REFERENCES categories(id),
            brand           VARCHAR(100),
            hsn_code        VARCHAR(10),
            unit            VARCHAR(20) DEFAULT 'PCS',
            gst_rate        NUMERIC(5,2) NOT NULL DEFAULT 18.00,
            purchase_price  NUMERIC(12,2) DEFAULT 0,
            selling_price   NUMERIC(12,2) DEFAULT 0,
            reorder_level   INTEGER DEFAULT 0,
            is_serialized   BOOLEAN NOT NULL DEFAULT FALSE,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE stock_ledger (
            id              BIGSERIAL PRIMARY KEY,
            product_id      UUID NOT NULL REFERENCES products(id),
            quantity        NUMERIC(12,2) NOT NULL,
            reference_type  VARCHAR(20) NOT NULL,
            reference_id    UUID,
            note            TEXT,
            created_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_stock_ledger_product ON stock_ledger(product_id);

        CREATE TABLE purchases (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            purchase_no     VARCHAR(30) UNIQUE NOT NULL,
            supplier_id     UUID NOT NULL REFERENCES suppliers(id),
            purchase_date   DATE NOT NULL DEFAULT CURRENT_DATE,
            supplier_invoice_no VARCHAR(50),
            subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
            total_gst       NUMERIC(12,2) NOT NULL DEFAULT 0,
            total_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
            paid_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
            payment_status  VARCHAR(10) NOT NULL DEFAULT 'UNPAID',
            notes           TEXT,
            created_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE purchase_items (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            purchase_id     UUID NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
            product_id      UUID NOT NULL REFERENCES products(id),
            quantity        NUMERIC(12,2) NOT NULL,
            unit_price      NUMERIC(12,2) NOT NULL,
            gst_rate        NUMERIC(5,2) NOT NULL,
            gst_amount      NUMERIC(12,2) NOT NULL,
            line_total      NUMERIC(12,2) NOT NULL
        );

        CREATE TABLE quotations (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            quotation_no    VARCHAR(30) UNIQUE NOT NULL,
            customer_id     UUID NOT NULL REFERENCES customers(id),
            quotation_date  DATE NOT NULL DEFAULT CURRENT_DATE,
            valid_till      DATE,
            subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
            total_gst       NUMERIC(12,2) NOT NULL DEFAULT 0,
            total_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
            status          VARCHAR(15) NOT NULL DEFAULT 'PENDING',
            converted_invoice_id UUID,
            notes           TEXT,
            created_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE quotation_items (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            quotation_id    UUID NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
            product_id      UUID REFERENCES products(id),
            description     VARCHAR(300),
            quantity        NUMERIC(12,2) NOT NULL DEFAULT 1,
            unit_price      NUMERIC(12,2) NOT NULL,
            gst_rate        NUMERIC(5,2) NOT NULL,
            gst_amount      NUMERIC(12,2) NOT NULL,
            line_total      NUMERIC(12,2) NOT NULL,
            customer_warranty_months INTEGER
        );

        CREATE TABLE invoices (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            invoice_no      VARCHAR(30) UNIQUE NOT NULL,
            customer_id     UUID NOT NULL REFERENCES customers(id),
            invoice_date    DATE NOT NULL DEFAULT CURRENT_DATE,
            quotation_id    UUID REFERENCES quotations(id),
            place_of_supply_state_code VARCHAR(2),
            is_interstate   BOOLEAN NOT NULL DEFAULT FALSE,
            subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
            total_cgst      NUMERIC(12,2) NOT NULL DEFAULT 0,
            total_sgst      NUMERIC(12,2) NOT NULL DEFAULT 0,
            total_igst      NUMERIC(12,2) NOT NULL DEFAULT 0,
            round_off       NUMERIC(6,2) NOT NULL DEFAULT 0,
            total_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
            paid_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
            payment_status  VARCHAR(10) NOT NULL DEFAULT 'UNPAID',
            invoice_type    VARCHAR(10) NOT NULL DEFAULT 'SALE',
            notes           TEXT,
            created_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE invoice_items (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            product_id      UUID REFERENCES products(id),
            description     VARCHAR(300),
            quantity        NUMERIC(12,2) NOT NULL DEFAULT 1,
            unit_price      NUMERIC(12,2) NOT NULL,
            gst_rate        NUMERIC(5,2) NOT NULL,
            cgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
            sgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
            igst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
            line_total      NUMERIC(12,2) NOT NULL
        );

        CREATE TABLE payments (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            invoice_id      UUID REFERENCES invoices(id),
            purchase_id     UUID REFERENCES purchases(id),
            amount          NUMERIC(12,2) NOT NULL,
            payment_mode    VARCHAR(15) NOT NULL DEFAULT 'CASH',
            payment_date    DATE NOT NULL DEFAULT CURRENT_DATE,
            reference_no    VARCHAR(50),
            notes           TEXT,
            created_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (invoice_id IS NOT NULL OR purchase_id IS NOT NULL)
        );

        CREATE TABLE amc_contracts (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            contract_no     VARCHAR(30) UNIQUE NOT NULL,
            customer_id     UUID NOT NULL REFERENCES customers(id),
            site_address    TEXT,
            start_date      DATE NOT NULL,
            end_date        DATE NOT NULL,
            visit_frequency VARCHAR(20) DEFAULT 'QUARTERLY',
            amount          NUMERIC(12,2) NOT NULL,
            gst_rate        NUMERIC(5,2) NOT NULL DEFAULT 18.00,
            coverage_notes  TEXT,
            status          VARCHAR(15) NOT NULL DEFAULT 'ACTIVE',
            linked_invoice_id UUID REFERENCES invoices(id),
            created_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE amc_service_visits (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            contract_id     UUID NOT NULL REFERENCES amc_contracts(id) ON DELETE CASCADE,
            scheduled_date  DATE NOT NULL,
            completed_date  DATE,
            technician_id   UUID REFERENCES users(id),
            status          VARCHAR(15) NOT NULL DEFAULT 'SCHEDULED',
            remarks         TEXT
        );

        CREATE TABLE part_warranties (
            id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            product_id                      UUID NOT NULL REFERENCES products(id),
            barcode                         VARCHAR(100) UNIQUE,
            status                          VARCHAR(20) NOT NULL DEFAULT 'IN_STOCK',
            purchase_id                     UUID REFERENCES purchases(id),
            purchase_item_id                UUID REFERENCES purchase_items(id),
            purchase_date                   DATE,
            supplier_warranty_months        INTEGER,
            supplier_warranty_valid_until   DATE,
            invoice_id                      UUID REFERENCES invoices(id),
            invoice_item_id                 UUID REFERENCES invoice_items(id),
            sale_date                       DATE,
            customer_warranty_months        INTEGER,
            customer_warranty_valid_until   DATE,
            notes                           TEXT,
            replaces_warranty_id            UUID REFERENCES part_warranties(id),
            replaced_by_warranty_id         UUID REFERENCES part_warranties(id),
            created_by                      UUID REFERENCES users(id),
            created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_part_warranties_product ON part_warranties(product_id);
        CREATE INDEX idx_part_warranties_replaces ON part_warranties(replaces_warranty_id);
        CREATE INDEX idx_part_warranties_barcode ON part_warranties(barcode);
        CREATE INDEX idx_part_warranties_status ON part_warranties(status);
        CREATE INDEX idx_part_warranties_supplier_valid ON part_warranties(supplier_warranty_valid_until);
        CREATE INDEX idx_part_warranties_customer_valid ON part_warranties(customer_warranty_valid_until);

        CREATE INDEX idx_invoices_customer ON invoices(customer_id);
        CREATE INDEX idx_invoices_date ON invoices(invoice_date);
        CREATE INDEX idx_purchases_supplier ON purchases(supplier_id);
        CREATE INDEX idx_amc_end_date ON amc_contracts(end_date);
        CREATE INDEX idx_quotations_customer ON quotations(customer_id);

        INSERT INTO store_profile (id, store_name) VALUES (1, 'My CCTV Store')
        ON CONFLICT (id) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS part_warranties CASCADE;
        DROP TABLE IF EXISTS amc_service_visits CASCADE;
        DROP TABLE IF EXISTS amc_contracts CASCADE;
        DROP TABLE IF EXISTS payments CASCADE;
        DROP TABLE IF EXISTS invoice_items CASCADE;
        DROP TABLE IF EXISTS invoices CASCADE;
        DROP TABLE IF EXISTS quotation_items CASCADE;
        DROP TABLE IF EXISTS quotations CASCADE;
        DROP TABLE IF EXISTS purchase_items CASCADE;
        DROP TABLE IF EXISTS purchases CASCADE;
        DROP TABLE IF EXISTS stock_ledger CASCADE;
        DROP TABLE IF EXISTS products CASCADE;
        DROP TABLE IF EXISTS categories CASCADE;
        DROP TABLE IF EXISTS customers CASCADE;
        DROP TABLE IF EXISTS suppliers CASCADE;
        DROP TABLE IF EXISTS store_profile CASCADE;
        DROP TABLE IF EXISTS users CASCADE;
        """
    )
