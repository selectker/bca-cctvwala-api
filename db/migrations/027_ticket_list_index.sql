-- v1.32.1: the ticket list sorts by newest first with no status filter; the existing (status, created_at) index can't
-- serve that sort. Idempotent.
CREATE INDEX IF NOT EXISTS idx_service_tickets_created ON service_tickets(created_at DESC, id);
