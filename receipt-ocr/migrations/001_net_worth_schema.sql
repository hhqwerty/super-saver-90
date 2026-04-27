-- =============================================================================
-- Migration 001: Net Worth Tracking Schema
-- Extends the existing `receipts` table with wealth management capabilities
-- Run: psql -U n8n -d finance -f 001_net_worth_schema.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Extend receipts table
-- ---------------------------------------------------------------------------
-- Add asset_id FK (nullable — most receipts are NOT asset transactions)
ALTER TABLE receipts
    ADD COLUMN IF NOT EXISTS asset_id INTEGER;

-- Extend transaction_type enum values (text column, just document valid values):
-- Existing: 'expense', 'income'
-- New:      'investment_buy'   → spending cash to acquire an asset
--           'investment_sell'  → receiving cash from selling an asset
--           'balance_init'     → opening balance (excluded from P&L charts)
COMMENT ON COLUMN receipts.transaction_type IS
    'expense | income | investment_buy | investment_sell | balance_init';

-- ---------------------------------------------------------------------------
-- 2. assets — portfolio holdings (quantity of each asset owned)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id           SERIAL PRIMARY KEY,
    asset_name   TEXT        NOT NULL,                    -- e.g. "Bitcoin", "SJC Gold", "VNM Stock"
    asset_type   TEXT        NOT NULL                     -- crypto | gold | stock | cash | real_estate
                     CHECK (asset_type IN ('crypto','gold','stock','cash','real_estate')),
    asset_code   TEXT        NOT NULL UNIQUE,             -- BTC, XAU_SJC, VNM  (links to asset_prices)
    quantity      NUMERIC(28,8)  NOT NULL DEFAULT 0,       -- current balance / amount held
    initial_price NUMERIC(28,2),                          -- what you paid (purchase cost per unit)
    currency      CHAR(3)      NOT NULL DEFAULT 'VND',    -- ISO 4217
    owner_name   TEXT        NOT NULL DEFAULT 'Joint',    -- Husband | Wife | Joint
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_assets_type     ON assets (asset_type);
CREATE INDEX IF NOT EXISTS idx_assets_owner    ON assets (owner_name);

-- Foreign key from receipts to assets (deferred so both rows can be inserted in one tx)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_receipts_asset'
    ) THEN
        ALTER TABLE receipts
            ADD CONSTRAINT fk_receipts_asset
            FOREIGN KEY (asset_id) REFERENCES assets (id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 3. asset_prices — latest market rates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_prices (
    asset_code    TEXT        PRIMARY KEY,                -- matches assets.asset_code
    price_vnd     NUMERIC(28,2),                         -- price in VND (may be null if USD-quoted)
    price_usd     NUMERIC(28,8),                         -- price in USD
    source        TEXT,                                   -- e.g. 'coingecko', 'sjc', 'manual'
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed with placeholder rows so the view always has a join target
INSERT INTO asset_prices (asset_code, price_vnd, price_usd, source)
VALUES
    ('VND',      1,          NULL,   'fixed'),
    ('USD',      NULL,       1,      'fixed'),
    ('BTC',      NULL,       NULL,   'coingecko'),
    ('ETH',      NULL,       NULL,   'coingecko'),
    ('XAU_SJC',  NULL,       NULL,   'sjc'),
    ('VNM',      NULL,       NULL,   'vndirect')
ON CONFLICT (asset_code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. financial_goals
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS financial_goals (
    id             SERIAL PRIMARY KEY,
    goal_name      TEXT           NOT NULL,               -- "New Apartment", "Emergency Fund"
    target_amount  NUMERIC(20,2)  NOT NULL,               -- in VND
    current_amount NUMERIC(20,2)  NOT NULL DEFAULT 0,     -- manually updated snapshot
    deadline       DATE,
    priority       INTEGER        NOT NULL DEFAULT 1,     -- lower = higher priority
    notes          TEXT,
    achieved       BOOLEAN        NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 5. asset_price_history — time-series for charting
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_price_history (
    id          SERIAL PRIMARY KEY,
    asset_code  TEXT           NOT NULL REFERENCES asset_prices (asset_code),
    price_vnd   NUMERIC(28,2),
    price_usd   NUMERIC(28,8),
    recorded_at TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_hist_code_time
    ON asset_price_history (asset_code, recorded_at DESC);

-- ---------------------------------------------------------------------------
-- 6. Trigger: auto-update updated_at
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_assets_updated_at      ON assets;
DROP TRIGGER IF EXISTS trg_goals_updated_at        ON financial_goals;

CREATE TRIGGER trg_assets_updated_at
    BEFORE UPDATE ON assets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_goals_updated_at
    BEFORE UPDATE ON financial_goals
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 7. VIEW: v_portfolio  — real-time portfolio value
-- ---------------------------------------------------------------------------
-- Each asset row * its current price. Cash assets use quantity directly.
-- Filters out rows with NULL prices (not yet fetched) but still shows them.
CREATE OR REPLACE VIEW v_portfolio AS
SELECT
    a.id,
    a.asset_name,
    a.asset_type,
    a.asset_code,
    a.quantity,
    a.currency,
    a.owner_name,
    ap.price_vnd,
    ap.price_usd,
    ap.updated_at                                          AS price_updated_at,
    -- Value in VND: prefer price_vnd; fall back to price_usd * ~25500 exchange rate
    CASE
        WHEN a.asset_type = 'cash' AND a.currency = 'VND' THEN a.quantity
        WHEN a.asset_type = 'cash' AND a.currency = 'USD' THEN a.quantity * COALESCE(ap.price_vnd, 25500)
        WHEN ap.price_vnd IS NOT NULL                       THEN a.quantity * ap.price_vnd
        WHEN ap.price_usd IS NOT NULL                       THEN a.quantity * ap.price_usd * 25500
        ELSE NULL
    END                                                    AS value_vnd
FROM assets a
LEFT JOIN asset_prices ap ON ap.asset_code = a.asset_code;

-- ---------------------------------------------------------------------------
-- 8. VIEW: v_net_worth  — single-row summary
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_net_worth AS
SELECT
    SUM(CASE WHEN asset_type = 'cash'     THEN COALESCE(value_vnd, 0) ELSE 0 END) AS liquid_vnd,
    SUM(CASE WHEN asset_type != 'cash'    THEN COALESCE(value_vnd, 0) ELSE 0 END) AS invested_vnd,
    SUM(COALESCE(value_vnd, 0))                                                     AS total_net_worth_vnd,
    COUNT(*) FILTER (WHERE value_vnd IS NULL)                                       AS assets_missing_price,
    NOW()                                                                            AS calculated_at
FROM v_portfolio;

-- ---------------------------------------------------------------------------
-- 9. VIEW: v_cashflow  — Income / Expense only (excludes balance_init & investments)
-- ---------------------------------------------------------------------------
-- This is what feeds the Monthly Income / Expense charts in Metabase.
-- It intentionally excludes 'balance_init' and 'investment_*' types.
CREATE OR REPLACE VIEW v_cashflow AS
SELECT *
FROM receipts
WHERE transaction_type IN ('income', 'expense')
  AND confirmed = TRUE;

-- ---------------------------------------------------------------------------
-- 10. VIEW: v_monthly_savings  — last 6 months average
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_monthly_savings AS
WITH monthly AS (
    SELECT
        DATE_TRUNC('month', date)                         AS month,
        SUM(CASE WHEN transaction_type = 'income'  THEN total_amount ELSE 0 END) AS income,
        SUM(CASE WHEN transaction_type = 'expense' THEN total_amount ELSE 0 END) AS expenses
    FROM v_cashflow
    WHERE date >= (CURRENT_DATE - INTERVAL '6 months')
    GROUP BY 1
)
SELECT
    month,
    income,
    expenses,
    income - expenses                                     AS savings
FROM monthly
ORDER BY month;

-- ---------------------------------------------------------------------------
-- Done
-- ---------------------------------------------------------------------------
\echo 'Migration 001 complete. Run SELECT * FROM v_net_worth; to verify.'
