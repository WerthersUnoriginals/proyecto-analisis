-- Fundamentals v3 evidence: provider attempts, verbatim SEC Company Facts,
-- EDGAR filing metadata, and the raw rows returned by each attempt.
-- Additive only: no existing table, view, or constraint is modified.
-- Every table is append-only; one attempt is written in one transaction.

BEGIN;

CREATE FUNCTION public.reject_evidence_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only evidence table %: % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE TABLE public.ingestion_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ingestion_runs_provider_check CHECK (provider IN ('SEC', 'YAHOO_FINANCE')),
    CONSTRAINT ingestion_runs_status_check CHECK (
        (status = 'SUCCESS' AND error_code IS NULL)
        OR (status = 'FAILED' AND error_code ~ '^[A-Z][A-Z0-9_]{0,63}$')
    ),
    CONSTRAINT ingestion_runs_time_check CHECK (started_at <= completed_at),
    CONSTRAINT ingestion_runs_item_count_check CHECK (item_count >= 0)
);

CREATE INDEX ingestion_runs_company_operation_idx
    ON public.ingestion_runs (company_id, operation, completed_at DESC);

CREATE TABLE public.sec_companyfacts_raw (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    cik TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    tag TEXT NOT NULL,
    unit TEXT NOT NULL,
    period_start DATE,
    period_end DATE NOT NULL,
    value NUMERIC(38, 10) NOT NULL,
    accession TEXT NOT NULL,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    form TEXT NOT NULL,
    filed_date DATE NOT NULL,
    frame TEXT,
    catalog_version TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sec_companyfacts_raw_period_check CHECK (period_start IS NULL OR period_start <= period_end),
    CONSTRAINT sec_companyfacts_raw_cik_check CHECK (cik ~ '^[0-9]{10}$')
);

CREATE UNIQUE INDEX sec_companyfacts_raw_identity_unique
    ON public.sec_companyfacts_raw (company_id, taxonomy, tag, unit, period_start, period_end, accession)
    NULLS NOT DISTINCT;

CREATE INDEX sec_companyfacts_raw_company_observed_idx
    ON public.sec_companyfacts_raw (company_id, observed_at);

CREATE TABLE public.sec_filings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    cik TEXT NOT NULL,
    accession TEXT NOT NULL,
    form TEXT NOT NULL,
    filing_date DATE NOT NULL,
    acceptance_at TIMESTAMPTZ,
    report_date DATE,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sec_filings_accession_unique UNIQUE (company_id, accession),
    CONSTRAINT sec_filings_acceptance_check CHECK (
        acceptance_at IS NULL OR acceptance_at::date >= filing_date - 1
    )
);

CREATE TABLE public.ingestion_run_items (
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    raw_table TEXT NOT NULL,
    raw_id BIGINT NOT NULL,
    PRIMARY KEY (run_id, raw_table, raw_id),
    CONSTRAINT ingestion_run_items_table_check CHECK (raw_table IN ('fundamentals_raw'))
);

CREATE TRIGGER ingestion_runs_append_only
    BEFORE UPDATE OR DELETE ON public.ingestion_runs
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER sec_companyfacts_raw_append_only
    BEFORE UPDATE OR DELETE ON public.sec_companyfacts_raw
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER sec_filings_append_only
    BEFORE UPDATE OR DELETE ON public.sec_filings
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER ingestion_run_items_append_only
    BEFORE UPDATE OR DELETE ON public.ingestion_run_items
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();

COMMIT;
