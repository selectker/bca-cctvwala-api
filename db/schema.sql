-- ============================================================
-- GST Billing & Inventory Management - PostgreSQL Schema
-- Target: AWS RDS (PostgreSQL 15+)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto"; -- for gen_random_uuid()

-- ---------- USERS (app/staff login, controlled fully by your Android app) ----------
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(120) NOT NULL,
    phone           VARCHAR(15) UNIQUE NOT NULL,
    email           VARCHAR(150) UNIQUE,
    password_hash   TEXT NOT NULL,
    role            VARCHAR(20) NOT NULL DEFAULT 'STAFF', -- OWNER, STAFF, TECHNICIAN
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    password_changed_at TIMESTAMPTZ,  -- tokens issued before this are rejected
    is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE,  -- the first registered account; only it can back up / restore the DB
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_users_single_platform_admin ON users ((TRUE)) WHERE is_platform_admin;

-- ---------- STORE / COMPANY PROFILE (for invoice header, GSTIN etc.) ----------
CREATE TABLE store_profile (
    id              SMALLINT PRIMARY KEY DEFAULT 1,
    store_name      VARCHAR(150) NOT NULL,
    gstin           VARCHAR(15),
    address         TEXT,
    state           VARCHAR(50),
    state_code      VARCHAR(2),      -- e.g. '29' for Karnataka - needed for CGST/SGST vs IGST
    phone           VARCHAR(15),
    email           VARCHAR(150),
    bank_name       VARCHAR(150),
    bank_account_no VARCHAR(30),
    bank_ifsc       VARCHAR(15),
    upi_id          VARCHAR(100),  -- shop UPI ID; prints a pay-by-QR code on invoices that still have a balance (v1.31.0)
    invoice_prefix  VARCHAR(10) DEFAULT 'INV',
    quotation_prefix VARCHAR(10) DEFAULT 'QUO',
    logo_key        VARCHAR(200),    -- R2 object key of the logo printed on invoices / quotations (see /store-profile/logo)
    logo_updated_at TIMESTAMPTZ,
    signature_key   VARCHAR(200),    -- R2 object key of the signature printed on invoices (see /store-profile/signature)
    signature_updated_at TIMESTAMPTZ,
    CONSTRAINT single_row CHECK (id = 1)
);

-- ---------- SUPPLIERS (wholesalers you procure from) ----------
CREATE TABLE suppliers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(150) NOT NULL,
    gstin           VARCHAR(15),
    phone           VARCHAR(15),
    email           VARCHAR(150),
    address         TEXT,
    state           VARCHAR(50),
    state_code      VARCHAR(2),
    opening_balance NUMERIC(12,2) DEFAULT 0, -- payable to supplier
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- CUSTOMERS ----------
CREATE TABLE customers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(150) NOT NULL,
    gstin           VARCHAR(15),           -- nullable, B2C vs B2B
    phone           VARCHAR(15),
    email           VARCHAR(150),
    address         TEXT,            -- billing address
    shipping_address TEXT,           -- delivery address; empty = same as billing (v1.24.0)
    state           VARCHAR(50),
    state_code      VARCHAR(2),
    customer_type   VARCHAR(10) DEFAULT 'B2C', -- B2B / B2C
    opening_balance NUMERIC(12,2) DEFAULT 0,   -- receivable from customer
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    credit_limit    NUMERIC(12,2) CHECK (credit_limit IS NULL OR credit_limit >= 0),  -- null = no limit (v1.30.0)
    payment_terms_days INTEGER CHECK (payment_terms_days IS NULL OR payment_terms_days BETWEEN 0 AND 365)  -- default terms for new invoices
);

-- ---------- PRODUCT CATEGORIES ----------
CREATE TABLE categories (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name    VARCHAR(100) NOT NULL UNIQUE  -- e.g. Camera, DVR/NVR, Cable, Connector, HDD, Power Supply
);

-- ---------- PRODUCTS (CCTV cameras, DVRs, cables, connectors, HDD, etc.) ----------
CREATE TABLE products (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sku             VARCHAR(50) UNIQUE,
    name            VARCHAR(200) NOT NULL,
    category_id     UUID REFERENCES categories(id),
    brand           VARCHAR(100),
    hsn_code        VARCHAR(10),               -- HSN for GST
    unit            VARCHAR(20) DEFAULT 'PCS',  -- PCS, MTR, BOX etc.
    gst_rate        NUMERIC(5,2) NOT NULL DEFAULT 18.00, -- % e.g. 18.00
    purchase_price  NUMERIC(12,2) DEFAULT 0,   -- last/avg purchase price (ex-GST)
    selling_price   NUMERIC(12,2) DEFAULT 0,   -- default selling price (ex-GST)
    reorder_level   INTEGER DEFAULT 0,
    is_serialized   BOOLEAN NOT NULL DEFAULT FALSE, -- TRUE = each unit carries its own barcode/serial (cameras, DVRs...); FALSE = bulk item (cable, connectors)
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- STOCK LEDGER (single source of truth for current stock; append-only) ----------
-- current stock for a product = SUM(quantity) where quantity is signed (+in / -out)
CREATE TABLE stock_ledger (
    id              BIGSERIAL PRIMARY KEY,
    product_id      UUID NOT NULL REFERENCES products(id),
    quantity        NUMERIC(12,2) NOT NULL,     -- +ve for IN, -ve for OUT
    reference_type  VARCHAR(20) NOT NULL,       -- PURCHASE, SALE, ADJUSTMENT, RETURN_IN, RETURN_OUT
    reference_id    UUID,                       -- purchase_id / sale_id / adjustment id
    note            TEXT,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_stock_ledger_product ON stock_ledger(product_id);

-- ---------- PURCHASES (from suppliers) ----------
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
    payment_status  VARCHAR(10) NOT NULL DEFAULT 'UNPAID', -- PAID, PARTIAL, UNPAID
    notes           TEXT,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE purchase_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_id     UUID NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    product_id      UUID NOT NULL REFERENCES products(id),
    quantity        NUMERIC(12,2) NOT NULL,
    unit_price      NUMERIC(12,2) NOT NULL,     -- ex-GST
    gst_rate        NUMERIC(5,2) NOT NULL,
    gst_amount      NUMERIC(12,2) NOT NULL,
    line_total      NUMERIC(12,2) NOT NULL      -- (qty*unit_price)+gst_amount
);

-- ---------- QUOTATIONS ----------
CREATE TABLE quotations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    quotation_no    VARCHAR(30) UNIQUE NOT NULL,
    customer_id     UUID NOT NULL REFERENCES customers(id),
    quotation_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_till      DATE,
    subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_gst       NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_discount  NUMERIC(12,2) NOT NULL DEFAULT 0,  -- sum of line discounts; already deducted from subtotal (v1.20.0)
    invoice_discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0,  -- as entered; the amount is already inside each line's discount_amount (v1.22.0)
    invoice_discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0,
    status          VARCHAR(15) NOT NULL DEFAULT 'PENDING', -- PENDING, ACCEPTED, REJECTED, CONVERTED
    converted_invoice_id UUID,
    notes           TEXT,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE quotation_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    quotation_id    UUID NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    product_id      UUID REFERENCES products(id),     -- nullable: allow free-text service line
    description     VARCHAR(300),
    quantity        NUMERIC(12,2) NOT NULL DEFAULT 1,
    unit_price      NUMERIC(12,2) NOT NULL,
    gst_rate        NUMERIC(5,2) NOT NULL,
    gst_amount      NUMERIC(12,2) NOT NULL,
    line_total      NUMERIC(12,2) NOT NULL,
    customer_warranty_months INTEGER,
    discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0,   -- per-line trade discount % (v1.20.0)
    discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0    -- rupees taken off before GST
);

-- ---------- SALES / INVOICES (to customers) ----------
CREATE TABLE invoices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_no      VARCHAR(30) UNIQUE NOT NULL,
    customer_id     UUID NOT NULL REFERENCES customers(id),
    invoice_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    quotation_id    UUID REFERENCES quotations(id),   -- if converted from a quotation
    place_of_supply_state_code VARCHAR(2),
    is_interstate   BOOLEAN NOT NULL DEFAULT FALSE,    -- decides IGST vs CGST+SGST
    subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_cgst      NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_sgst      NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_igst      NUMERIC(12,2) NOT NULL DEFAULT 0,
    round_off       NUMERIC(6,2) NOT NULL DEFAULT 0,
    total_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_discount  NUMERIC(12,2) NOT NULL DEFAULT 0,  -- sum of line discounts; already deducted from subtotal (v1.20.0)
    invoice_discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0,  -- as entered on the invoice; the amount is already inside each line's discount_amount (v1.21.0)
    invoice_discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0,
    paid_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    payment_status  VARCHAR(10) NOT NULL DEFAULT 'UNPAID',
    invoice_type    VARCHAR(10) NOT NULL DEFAULT 'SALE', -- SALE, SERVICE, AMC
    billing_address  TEXT,                              -- copied from the customer when the invoice is made; printed on the PDF (v1.24.0)
    shipping_address TEXT,                              -- delivery address, same idea
    notes           TEXT,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_date        DATE,                                -- null = due on the invoice date (v1.30.0)
    payment_terms_days INTEGER CHECK (payment_terms_days IS NULL OR payment_terms_days BETWEEN 0 AND 365)
);

CREATE TABLE invoice_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    product_id      UUID REFERENCES products(id),      -- nullable for service/installation lines
    description     VARCHAR(300),
    quantity        NUMERIC(12,2) NOT NULL DEFAULT 1,
    unit_price      NUMERIC(12,2) NOT NULL,
    gst_rate        NUMERIC(5,2) NOT NULL,
    cgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    sgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    igst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    line_total      NUMERIC(12,2) NOT NULL,
    hsn_code        VARCHAR(10),                        -- HSN / SAC printed on the invoice, copied from the product at sale time (v1.19.0)
    discount_percent NUMERIC(5,2)  NOT NULL DEFAULT 0,   -- per-line trade discount % (v1.20.0)
    discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0    -- rupees taken off before GST
);

CREATE TABLE payments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID REFERENCES invoices(id),
    purchase_id     UUID REFERENCES purchases(id),
    amount          NUMERIC(12,2) NOT NULL,
    payment_mode    VARCHAR(15) NOT NULL DEFAULT 'CASH', -- CASH, UPI, BANK, CHEQUE, CARD
    payment_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    reference_no    VARCHAR(50),
    notes           TEXT,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (invoice_id IS NOT NULL OR purchase_id IS NOT NULL),
    reversed_at     TIMESTAMPTZ,                         -- set on a payment that was reversed (v1.30.0)
    reversed_by     UUID REFERENCES users(id),
    reversal_reason VARCHAR(500),
    reverses_payment_id UUID REFERENCES payments(id)     -- set on the negative row that reverses another payment
);

-- ---------- AMC (Annual Maintenance Contracts) ----------
CREATE TABLE amc_contracts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_no     VARCHAR(30) UNIQUE NOT NULL,
    customer_id     UUID NOT NULL REFERENCES customers(id),
    site_address    TEXT,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    visit_frequency VARCHAR(20) DEFAULT 'QUARTERLY', -- MONTHLY, QUARTERLY, HALF_YEARLY, YEARLY
    amount          NUMERIC(12,2) NOT NULL,
    gst_rate        NUMERIC(5,2) NOT NULL DEFAULT 18.00,
    coverage_notes  TEXT,          -- what's covered: cameras/DVR/cabling etc.
    status          VARCHAR(15) NOT NULL DEFAULT 'ACTIVE', -- ACTIVE, EXPIRED, CANCELLED
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
    status          VARCHAR(15) NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED, COMPLETED, MISSED
    remarks         TEXT
);

-- ---------- PART WARRANTY TRACKING (serialized units with physical barcode) ----------
-- One row per physical unit of a part that carries its own printed barcode/serial number,
-- so both the manufacturer/supplier warranty (from purchase) and the warranty we extend to
-- the customer (from sale) can be tracked against that exact physical unit.
CREATE TABLE part_warranties (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id                      UUID NOT NULL REFERENCES products(id),
    barcode                         VARCHAR(100) UNIQUE,  -- actual physical barcode/serial printed on the part
    status                          VARCHAR(20) NOT NULL DEFAULT 'IN_STOCK', -- IN_STOCK, SOLD, RETURNED, CLAIMED, REPLACED, SCRAPPED
    -- purchase side: warranty the supplier/manufacturer gives us
    purchase_id                     UUID REFERENCES purchases(id),
    purchase_item_id                UUID REFERENCES purchase_items(id),
    purchase_date                   DATE,
    supplier_warranty_months        INTEGER,
    supplier_warranty_valid_until   DATE,
    -- sale side: warranty we extend to the customer
    invoice_id                      UUID REFERENCES invoices(id),
    invoice_item_id                 UUID REFERENCES invoice_items(id),
    sale_date                       DATE,
    customer_warranty_months        INTEGER,
    customer_warranty_valid_until   DATE,
    notes                           TEXT,
    -- RMA / warranty-claim trail: if this unit was swapped for another one under warranty,
    -- these point at the two sides of that swap (see /warranties/{id}/claim and /replace).
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

-- ---------- USEFUL INDEXES ----------
CREATE INDEX idx_invoices_customer ON invoices(customer_id);
CREATE INDEX idx_invoices_date ON invoices(invoice_date);
CREATE INDEX idx_purchases_supplier ON purchases(supplier_id);
CREATE INDEX idx_amc_end_date ON amc_contracts(end_date);
CREATE INDEX idx_quotations_customer ON quotations(customer_id);

-- ---------- SERVICE TICKETS (owner raises, staff/technician attends, owner closes; v1.8) ----------
CREATE TABLE service_tickets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_no       VARCHAR(30) UNIQUE NOT NULL,
    customer_id     UUID NOT NULL REFERENCES customers(id),
    service_type    VARCHAR(12) NOT NULL CHECK (service_type IN ('IN_WARRANTY', 'PAID', 'AMC')),
    title           VARCHAR(200) NOT NULL,
    description     TEXT,
    site_address    TEXT,
    warranty_id     UUID REFERENCES part_warranties(id),       -- optional: the tracked unit this request is about
    amc_id          UUID REFERENCES amc_contracts(id),         -- set on (and only on) an AMC ticket: the contract that covers it (v1.29.0)
    charge_amount   NUMERIC(12,2),                              -- optional estimate (ex-GST), pre-fills the invoice line
    gst_rate        NUMERIC(5,2) NOT NULL DEFAULT 18.00,
    status          VARCHAR(12) NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN', 'RESCHEDULED', 'ATTENDED', 'CLOSED', 'CANCELLED')),
    assigned_to     UUID REFERENCES users(id),                  -- NULL = any technician / staff may pick it up
    scheduled_date  DATE,
    attended_at     TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    invoice_id      UUID REFERENCES invoices(id),               -- set when billed on a SERVICE invoice
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT service_tickets_amc_link_check CHECK ((service_type = 'AMC') = (amc_id IS NOT NULL))
);

-- Append-only timeline: CREATED, UPDATED, RESCHEDULED, ATTENDED, CLOSED, CANCELLED.
CREATE TABLE service_ticket_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    event_type      VARCHAR(12) NOT NULL,
    note            TEXT,
    scheduled_date  DATE,                                       -- the new date, on RESCHEDULED
    actor_id        UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Completion photos. The bytes live in Cloudflare R2, not in the database.
CREATE TABLE service_ticket_images (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    event_id        UUID REFERENCES service_ticket_events(id) ON DELETE CASCADE,
    storage_key     VARCHAR(200) NOT NULL,                      -- object key in the R2 photo bucket
    content_type    VARCHAR(40) NOT NULL,
    size_bytes      INTEGER NOT NULL,
    uploaded_by     UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Customer sign-off on a completed visit (v1.29.0): a signature and / or an acknowledgement with the customer's name, or the
-- reason it couldn't be obtained. One row per visit event (ATTENDED, or a later ADDENDUM). The PNG lives in R2.
CREATE TABLE service_ticket_signoffs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id           UUID NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    event_id            UUID NOT NULL UNIQUE REFERENCES service_ticket_events(id) ON DELETE CASCADE,
    customer_name       VARCHAR(100),
    acknowledged        BOOLEAN NOT NULL DEFAULT FALSE,
    signature_key       VARCHAR(200),                           -- object key of the PNG in R2
    not_obtained_reason VARCHAR(300),
    captured_by         UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (acknowledged OR signature_key IS NOT NULL OR not_obtained_reason IS NOT NULL),
    CHECK (NOT (acknowledged OR signature_key IS NOT NULL) OR (customer_name IS NOT NULL AND not_obtained_reason IS NULL))
);

CREATE INDEX idx_service_tickets_customer ON service_tickets(customer_id);
CREATE INDEX idx_service_tickets_status_created ON service_tickets(status, created_at DESC);
CREATE INDEX idx_service_tickets_assigned ON service_tickets(assigned_to);
CREATE INDEX idx_service_tickets_invoice ON service_tickets(invoice_id) WHERE invoice_id IS NOT NULL;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_service_tickets_no_trgm ON service_tickets USING gin (ticket_no gin_trgm_ops);
CREATE INDEX idx_service_ticket_events_ticket ON service_ticket_events(ticket_id, created_at);
CREATE INDEX idx_service_ticket_images_ticket ON service_ticket_images(ticket_id);
CREATE INDEX idx_service_tickets_amc ON service_tickets(amc_id) WHERE amc_id IS NOT NULL;
CREATE INDEX idx_service_ticket_signoffs_ticket ON service_ticket_signoffs(ticket_id, created_at);

-- ---------- AUDIT LOG (v1.10): append-only history of who did what ----------
-- ---------- EXPENSES (v1.18.0) ----------
CREATE TABLE IF NOT EXISTS expenses (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    expense_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    category      VARCHAR(60) NOT NULL,
    amount        NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    payment_mode  VARCHAR(15) NOT NULL DEFAULT 'CASH',   -- CASH, UPI, BANK, CHEQUE, CARD
    paid_to       VARCHAR(120),                          -- vendor / person (optional)
    reference_no  VARCHAR(50),                           -- cheque no / UTR / bill no
    notes         TEXT,
    created_by    UUID REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (expense_date DESC, id);
CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses (lower(category), expense_date DESC);

CREATE TABLE audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      UUID,                       -- NULL for an attempt by someone who isn't a known account. No foreign key: a restore replaces users, never this log
    user_name    VARCHAR(120),               -- name / role as they were at the time
    user_role    VARCHAR(20),
    action       VARCHAR(40) NOT NULL,       -- CREATE, UPDATE, DELETE, CANCEL, PAYMENT, LOGIN, ...
    entity_type  VARCHAR(40) NOT NULL,       -- INVOICE, CUSTOMER, USER, ...
    entity_id    TEXT,
    summary      TEXT NOT NULL,              -- e.g. 'Cancelled invoice INV-0012'
    details      JSONB,                      -- sanitized request body etc. (never passwords / PINs)
    ip_address   VARCHAR(64)
);

CREATE INDEX idx_audit_logs_created ON audit_logs(created_at DESC, id DESC);
CREATE INDEX idx_audit_logs_entity ON audit_logs(entity_type, entity_id, created_at DESC);
CREATE INDEX idx_audit_logs_user ON audit_logs(user_id, created_at DESC);
CREATE INDEX idx_audit_logs_login_failed ON audit_logs ((details->>'identifier'), created_at DESC) WHERE action = 'LOGIN_FAILED';
CREATE INDEX idx_audit_logs_login_ok ON audit_logs (user_id, ip_address, created_at DESC) WHERE action = 'LOGIN';

CREATE OR REPLACE FUNCTION trg_audit_logs_append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_logs_append_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION trg_audit_logs_append_only();

-- TRUNCATE is refused too: a database restore leaves this table alone (it only loads a backup's audit rows into an EMPTY log).
CREATE OR REPLACE FUNCTION trg_audit_logs_no_truncate() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs cannot be truncated';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_logs_no_truncate
    BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION trg_audit_logs_no_truncate();

-- ---------- SESSIONS (v1.12): one row per sign-in; a token only works while its row is active ----------
-- No foreign key to users on purpose: a restore truncates users, and sessions are neither backed up nor restored.
CREATE TABLE auth_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    ip_address  VARCHAR(64),
    user_agent  VARCHAR(200)
);
CREATE INDEX idx_auth_sessions_user_active ON auth_sessions(user_id) WHERE revoked_at IS NULL;
CREATE INDEX idx_auth_sessions_expires ON auth_sessions(expires_at);

-- Sign-in failure counters (v1.13.2). Pruned automatically; neither backed up nor restored.
CREATE TABLE login_failures (
    subject     VARCHAR(80) NOT NULL,
    ip_address  VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_login_failures_subject ON login_failures(subject, created_at DESC);
CREATE INDEX idx_login_failures_created ON login_failures(created_at);

-- ---------- SEED store profile row ----------
INSERT INTO store_profile (id, store_name) VALUES (1, 'BCAcctvWala')
ON CONFLICT (id) DO NOTHING;

-- v1.30.0: one reversal per payment (the overdue index needs invoices.status; migration 025 adds it)
CREATE UNIQUE INDEX uq_payments_reverses ON payments (reverses_payment_id) WHERE reverses_payment_id IS NOT NULL;

-- v1.37.0: walk-in customer (see db/migrations/028_walk_in_customer.sql)
ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_walk_in BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE invoices  ADD COLUMN IF NOT EXISTS buyer_name  VARCHAR(150);
ALTER TABLE invoices  ADD COLUMN IF NOT EXISTS buyer_phone VARCHAR(30);
CREATE UNIQUE INDEX IF NOT EXISTS uq_customers_single_walk_in ON customers (is_walk_in) WHERE is_walk_in;

-- v1.38.0: credit notes (sales returns) and debit notes (purchase returns). Idempotent.
CREATE TABLE IF NOT EXISTS credit_notes (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credit_note_no              VARCHAR(30) UNIQUE NOT NULL,
    invoice_id                  UUID NOT NULL REFERENCES invoices(id),
    customer_id                 UUID NOT NULL REFERENCES customers(id),
    note_date                   DATE NOT NULL DEFAULT CURRENT_DATE,
    reason                      VARCHAR(500) NOT NULL,
    place_of_supply_state_code  VARCHAR(2),
    is_interstate               BOOLEAN NOT NULL DEFAULT FALSE,
    subtotal                    NUMERIC(12,2) NOT NULL DEFAULT 0,   -- taxable value returned
    total_cgst                  NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_sgst                  NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_igst                  NUMERIC(12,2) NOT NULL DEFAULT 0,
    round_off                   NUMERIC(6,2)  NOT NULL DEFAULT 0,
    total_amount                NUMERIC(12,2) NOT NULL DEFAULT 0,
    applied_to_invoice          NUMERIC(12,2) NOT NULL DEFAULT 0,   -- the part that cut what the customer still owed on the invoice
    refund_amount               NUMERIC(12,2) NOT NULL DEFAULT 0,   -- the part handed back to the customer
    refund_mode                 VARCHAR(15),
    restock                     BOOLEAN NOT NULL DEFAULT TRUE,      -- goods came back into stock (false = defective / scrapped)
    status                      VARCHAR(10) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CANCELLED')),
    cancelled_at                TIMESTAMPTZ,
    cancel_reason               VARCHAR(500),
    cancelled_by                UUID REFERENCES users(id),
    notes                       TEXT,
    created_by                  UUID REFERENCES users(id),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_credit_notes_invoice  ON credit_notes(invoice_id);
CREATE INDEX IF NOT EXISTS idx_credit_notes_customer ON credit_notes(customer_id);
CREATE INDEX IF NOT EXISTS idx_credit_notes_date     ON credit_notes(note_date);

CREATE TABLE IF NOT EXISTS credit_note_items (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credit_note_id   UUID NOT NULL REFERENCES credit_notes(id) ON DELETE CASCADE,
    invoice_item_id  UUID NOT NULL REFERENCES invoice_items(id),
    product_id       UUID REFERENCES products(id),
    description      VARCHAR(300),
    hsn_code         VARCHAR(10),
    quantity         NUMERIC(12,2) NOT NULL,
    unit_price       NUMERIC(12,2) NOT NULL,
    gst_rate         NUMERIC(5,2)  NOT NULL,
    discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0,
    taxable_value    NUMERIC(12,2) NOT NULL,
    cgst_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
    sgst_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
    igst_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
    line_total       NUMERIC(12,2) NOT NULL,
    barcodes         TEXT[],                 -- serial numbers that came back
    unit_snapshot    JSONB                   -- their sale / warranty details, so cancelling the note can restore them
);
CREATE INDEX IF NOT EXISTS idx_credit_note_items_note ON credit_note_items(credit_note_id);
CREATE INDEX IF NOT EXISTS idx_credit_note_items_item ON credit_note_items(invoice_item_id);

CREATE TABLE IF NOT EXISTS debit_notes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debit_note_no       VARCHAR(30) UNIQUE NOT NULL,
    purchase_id         UUID NOT NULL REFERENCES purchases(id),
    supplier_id         UUID NOT NULL REFERENCES suppliers(id),
    note_date           DATE NOT NULL DEFAULT CURRENT_DATE,
    reason              VARCHAR(500) NOT NULL,
    subtotal            NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_gst           NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_amount        NUMERIC(12,2) NOT NULL DEFAULT 0,
    applied_to_purchase NUMERIC(12,2) NOT NULL DEFAULT 0,   -- the part that cut what we still owed the supplier
    refund_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,   -- the part the supplier paid back to us
    refund_mode         VARCHAR(15),
    status              VARCHAR(10) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CANCELLED')),
    cancelled_at        TIMESTAMPTZ,
    cancel_reason       VARCHAR(500),
    cancelled_by        UUID REFERENCES users(id),
    notes               TEXT,
    created_by          UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_debit_notes_purchase ON debit_notes(purchase_id);
CREATE INDEX IF NOT EXISTS idx_debit_notes_supplier ON debit_notes(supplier_id);
CREATE INDEX IF NOT EXISTS idx_debit_notes_date     ON debit_notes(note_date);

CREATE TABLE IF NOT EXISTS debit_note_items (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debit_note_id    UUID NOT NULL REFERENCES debit_notes(id) ON DELETE CASCADE,
    purchase_item_id UUID NOT NULL REFERENCES purchase_items(id),
    product_id       UUID NOT NULL REFERENCES products(id),
    quantity         NUMERIC(12,2) NOT NULL,
    unit_price       NUMERIC(12,2) NOT NULL,
    gst_rate         NUMERIC(5,2)  NOT NULL,
    taxable_value    NUMERIC(12,2) NOT NULL,
    gst_amount       NUMERIC(12,2) NOT NULL,
    line_total       NUMERIC(12,2) NOT NULL,
    barcodes         TEXT[]
);
CREATE INDEX IF NOT EXISTS idx_debit_note_items_note ON debit_note_items(debit_note_id);
CREATE INDEX IF NOT EXISTS idx_debit_note_items_item ON debit_note_items(purchase_item_id);

-- the payment rows a note books on its invoice / purchase (so cancelling the note can undo exactly those)
ALTER TABLE payments ADD COLUMN IF NOT EXISTS credit_note_id UUID REFERENCES credit_notes(id);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS debit_note_id  UUID REFERENCES debit_notes(id);

-- v1.38.0: the scanned / photographed supplier invoice saved with a purchase (stored like the shop logo)
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS attachment_key  VARCHAR(300);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS attachment_type VARCHAR(100);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS attachment_name VARCHAR(200);

-- v1.38.1: optional cost per unit (before GST) on a custom (no product) invoice line, so profit-loss is right for it. Idempotent.
ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS cost_price NUMERIC(12,2) CHECK (cost_price IS NULL OR cost_price >= 0);
