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
