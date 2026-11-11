-- v1.18.0: business expenses (rent, salary, travel...), so the app can show real profit, not just sales against purchases.
-- Safe to run more than once. Amounts are the total paid (including any GST).
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
