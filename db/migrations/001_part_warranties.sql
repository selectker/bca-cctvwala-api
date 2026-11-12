-- ============================================================
-- Migration: part warranty tracking (barcode-tracked units)
-- Safe to run against an existing database that was set up from
-- an earlier copy of schema.sql — it only adds the new table.
-- Already-applied? This is idempotent (IF NOT EXISTS everywhere).
-- ============================================================

CREATE TABLE IF NOT EXISTS part_warranties (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id                      UUID NOT NULL REFERENCES products(id),
    barcode                         VARCHAR(100) UNIQUE,  -- actual physical barcode/serial printed on the part
    status                          VARCHAR(15) NOT NULL DEFAULT 'IN_STOCK', -- IN_STOCK, SOLD, RETURNED, CLAIMED, SCRAPPED
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
    created_by                      UUID REFERENCES users(id),
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_part_warranties_product ON part_warranties(product_id);
CREATE INDEX IF NOT EXISTS idx_part_warranties_barcode ON part_warranties(barcode);
CREATE INDEX IF NOT EXISTS idx_part_warranties_status ON part_warranties(status);
CREATE INDEX IF NOT EXISTS idx_part_warranties_supplier_valid ON part_warranties(supplier_warranty_valid_until);
CREATE INDEX IF NOT EXISTS idx_part_warranties_customer_valid ON part_warranties(customer_warranty_valid_until);
