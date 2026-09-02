-- CAN SLIM+ — esquema inicial de fundamentales
-- Separación entre dato bruto (auditable) y dato trimestral normalizado (consumible por algoritmos).

BEGIN;

CREATE TABLE IF NOT EXISTS fundamentals_raw (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    company_id BIGINT NOT NULL
        REFERENCES companies(id)
        ON DELETE RESTRICT,

    source TEXT NOT NULL,
    metric TEXT NOT NULL,

    period_start DATE,
    period_end DATE NOT NULL,
    filed_date DATE,

    fiscal_year INTEGER,
    fiscal_quarter SMALLINT,
    form_type TEXT,

    value NUMERIC(30, 8) NOT NULL,
    unit TEXT,
    currency TEXT,

    xbrl_tag TEXT,
    source_record_id TEXT,
    source_payload JSONB,

    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fundamentals_raw_metric_check
        CHECK (metric IN (
            'EPS_DILUTED',
            'REVENUE',
            'NET_INCOME',
            'DILUTED_SHARES'
        )),

    CONSTRAINT fundamentals_raw_fiscal_quarter_check
        CHECK (fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4),

    CONSTRAINT fundamentals_raw_period_check
        CHECK (period_start IS NULL OR period_start <= period_end)
);

-- Evita guardar repetidamente el mismo hecho de una misma fuente.
-- COALESCE permite deduplicar también cuando Yahoo u otra fuente no aporta
-- filed_date, tag o identificador externo.
CREATE UNIQUE INDEX IF NOT EXISTS fundamentals_raw_dedup_unique
    ON fundamentals_raw (
        company_id,
        source,
        metric,
        period_end,
        COALESCE(xbrl_tag, ''),
        COALESCE(filed_date, DATE '0001-01-01'),
        COALESCE(source_record_id, '')
    );

CREATE INDEX IF NOT EXISTS fundamentals_raw_company_period_idx
    ON fundamentals_raw (company_id, period_end DESC);

CREATE INDEX IF NOT EXISTS fundamentals_raw_company_metric_period_idx
    ON fundamentals_raw (company_id, metric, period_end DESC);

CREATE INDEX IF NOT EXISTS fundamentals_raw_filed_date_idx
    ON fundamentals_raw (filed_date DESC)
    WHERE filed_date IS NOT NULL;


CREATE TABLE IF NOT EXISTS fundamentals_quarterly (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    company_id BIGINT NOT NULL
        REFERENCES companies(id)
        ON DELETE RESTRICT,

    fiscal_year INTEGER,
    fiscal_quarter SMALLINT,

    period_start DATE,
    period_end DATE NOT NULL,
    latest_filed_date DATE,
    form_type TEXT,

    eps_diluted NUMERIC(20, 8),
    revenue NUMERIC(30, 2),
    net_income NUMERIC(30, 2),
    diluted_shares NUMERIC(30, 2),

    eps_source TEXT,
    revenue_source TEXT,
    net_income_source TEXT,
    diluted_shares_source TEXT,

    eps_xbrl_tag TEXT,
    revenue_xbrl_tag TEXT,
    net_income_xbrl_tag TEXT,
    diluted_shares_xbrl_tag TEXT,

    eps_raw_id BIGINT REFERENCES fundamentals_raw(id) ON DELETE SET NULL,
    revenue_raw_id BIGINT REFERENCES fundamentals_raw(id) ON DELETE SET NULL,
    net_income_raw_id BIGINT REFERENCES fundamentals_raw(id) ON DELETE SET NULL,
    diluted_shares_raw_id BIGINT REFERENCES fundamentals_raw(id) ON DELETE SET NULL,

    shares_quality TEXT,
    data_quality TEXT,
    data_integrity TEXT,

    normalized_by_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fundamentals_quarterly_company_period_unique
        UNIQUE (company_id, period_end),

    CONSTRAINT fundamentals_quarterly_fiscal_quarter_check
        CHECK (fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4),

    CONSTRAINT fundamentals_quarterly_period_check
        CHECK (period_start IS NULL OR period_start <= period_end)
);

CREATE INDEX IF NOT EXISTS fundamentals_quarterly_company_period_idx
    ON fundamentals_quarterly (company_id, period_end DESC);

CREATE INDEX IF NOT EXISTS fundamentals_quarterly_latest_filed_idx
    ON fundamentals_quarterly (latest_filed_date DESC)
    WHERE latest_filed_date IS NOT NULL;

COMMIT;
