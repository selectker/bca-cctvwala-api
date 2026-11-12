-- ============================================================
-- Migration: warranty claim/replace (RMA) trail
-- Lets a claimed unit be swapped for a replacement unit while keeping
-- the customer's invoice link and remaining warranty period intact.
-- Safe to re-run (idempotent).
-- ============================================================

ALTER TABLE part_warranties
    ALTER COLUMN status TYPE VARCHAR(20);

ALTER TABLE part_warranties
    ADD COLUMN IF NOT EXISTS replaces_warranty_id UUID REFERENCES part_warranties(id),
    ADD COLUMN IF NOT EXISTS replaced_by_warranty_id UUID REFERENCES part_warranties(id);

CREATE INDEX IF NOT EXISTS idx_part_warranties_replaces ON part_warranties(replaces_warranty_id);
