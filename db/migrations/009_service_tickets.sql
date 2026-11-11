-- ============================================================
-- Migration 009: service tickets (v1.8)
--
-- A customer's service request, raised by the OWNER as either an in-warranty
-- or a paid service. STAFF / TECHNICIAN logins can only mark it attended
-- (with completion photos) or reschedule it, and the OWNER reads the timeline
-- and closes it. A PAID ticket that has been attended can be picked on a
-- SERVICE invoice (service_tickets.invoice_id records which one).
-- Safe to re-run.
-- ============================================================

CREATE TABLE IF NOT EXISTS service_tickets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_no       VARCHAR(30) UNIQUE NOT NULL,
    customer_id     UUID NOT NULL REFERENCES customers(id),
    service_type    VARCHAR(12) NOT NULL CHECK (service_type IN ('IN_WARRANTY', 'PAID')),
    title           VARCHAR(200) NOT NULL,
    description     TEXT,
    site_address    TEXT,
    warranty_id     UUID REFERENCES part_warranties(id),       -- optional: the tracked unit this request is about
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
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only timeline: CREATED, UPDATED, RESCHEDULED, ATTENDED, CLOSED, CANCELLED.
CREATE TABLE IF NOT EXISTS service_ticket_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    event_type      VARCHAR(12) NOT NULL,
    note            TEXT,
    scheduled_date  DATE,                                       -- the new date, on RESCHEDULED
    actor_id        UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Completion photos. The bytes live in Cloudflare R2, not in the database.
CREATE TABLE IF NOT EXISTS service_ticket_images (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    event_id        UUID REFERENCES service_ticket_events(id) ON DELETE CASCADE,
    storage_key     VARCHAR(200) NOT NULL,                      -- object key in the R2 photo bucket
    content_type    VARCHAR(40) NOT NULL,
    size_bytes      INTEGER NOT NULL,
    uploaded_by     UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_service_tickets_customer ON service_tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_service_tickets_status_created ON service_tickets(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_service_tickets_assigned ON service_tickets(assigned_to);
CREATE INDEX IF NOT EXISTS idx_service_tickets_invoice ON service_tickets(invoice_id) WHERE invoice_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_service_tickets_no_trgm ON service_tickets USING gin (ticket_no gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_service_ticket_events_ticket ON service_ticket_events(ticket_id, created_at);
CREATE INDEX IF NOT EXISTS idx_service_ticket_images_ticket ON service_ticket_images(ticket_id);
