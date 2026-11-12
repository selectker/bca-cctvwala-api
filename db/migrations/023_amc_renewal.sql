-- v1.26.0: AMC renewal. A renewed contract is a new contract that points back at the one it renews.
-- Safe to run more than once.
ALTER TABLE amc_contracts ADD COLUMN IF NOT EXISTS renewed_from_id UUID REFERENCES amc_contracts(id);
CREATE INDEX IF NOT EXISTS idx_amc_renewed_from ON amc_contracts(renewed_from_id) WHERE renewed_from_id IS NOT NULL;
