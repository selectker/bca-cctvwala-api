-- v1.29.0: (1) AMC-covered service tickets, (2) customer sign-off on completed tickets.
-- Safe to run more than once.

-- 1. A ticket can be raised under an AMC contract. Such a ticket has service_type 'AMC' and points at the contract;
--    the two always go together (no AMC ticket without a contract, no contract on any other kind of ticket).
ALTER TABLE service_tickets ADD COLUMN IF NOT EXISTS amc_id UUID REFERENCES amc_contracts(id);
ALTER TABLE service_tickets DROP CONSTRAINT IF EXISTS service_tickets_service_type_check;
ALTER TABLE service_tickets ADD CONSTRAINT service_tickets_service_type_check
    CHECK (service_type IN ('IN_WARRANTY', 'PAID', 'AMC'));
ALTER TABLE service_tickets DROP CONSTRAINT IF EXISTS service_tickets_amc_link_check;
ALTER TABLE service_tickets ADD CONSTRAINT service_tickets_amc_link_check
    CHECK ((service_type = 'AMC') = (amc_id IS NOT NULL));
CREATE INDEX IF NOT EXISTS idx_service_tickets_amc ON service_tickets(amc_id) WHERE amc_id IS NOT NULL;

-- 2. What the customer said when the work was done: a signature and / or an acknowledgement, with their name, or the
--    reason the technician couldn't get one. One row per visit event (ATTENDED, or a later ADDENDUM). The signature image
--    lives in Cloudflare R2, not in the database.
CREATE TABLE IF NOT EXISTS service_ticket_signoffs (
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
CREATE INDEX IF NOT EXISTS idx_service_ticket_signoffs_ticket ON service_ticket_signoffs(ticket_id, created_at);
