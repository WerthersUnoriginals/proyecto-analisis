-- I evidence (spec 2026-10-03-institutional-i-v1 §3): SEC Form 13F data sets,
-- every 13F-HR/13F-HR/A submission, the literal holdings of tracked CUSIPs,
-- and the CUSIP used per company. Additive only; all tables append-only.

BEGIN;

CREATE TABLE public.sec_13f_datasets (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sec_13f_datasets_sha_check CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE UNIQUE INDEX sec_13f_datasets_identity_unique ON public.sec_13f_datasets (file_name, sha256);

CREATE TABLE public.sec_13f_filings (
    accession TEXT PRIMARY KEY,
    dataset_id BIGINT NOT NULL REFERENCES public.sec_13f_datasets (id) ON DELETE RESTRICT,
    filer_cik TEXT NOT NULL,
    filing_date DATE NOT NULL,
    submission_type TEXT NOT NULL,
    period_of_report DATE NOT NULL,
    amendment_type TEXT,
    CONSTRAINT sec_13f_filings_type_check CHECK (submission_type IN ('13F-HR', '13F-HR/A')),
    CONSTRAINT sec_13f_filings_cik_check CHECK (filer_cik ~ '^[0-9]{10}$')
);

CREATE INDEX sec_13f_filings_period_idx ON public.sec_13f_filings (period_of_report, filing_date);

CREATE TABLE public.sec_13f_holdings (
    accession TEXT NOT NULL REFERENCES public.sec_13f_filings (accession) ON DELETE RESTRICT,
    infotable_sk TEXT NOT NULL,
    dataset_id BIGINT NOT NULL REFERENCES public.sec_13f_datasets (id) ON DELETE RESTRICT,
    cusip TEXT NOT NULL,
    name_of_issuer TEXT NOT NULL,
    title_of_class TEXT,
    value NUMERIC(24, 2) NOT NULL,
    shares NUMERIC(24, 4) NOT NULL,
    shares_type TEXT NOT NULL,
    put_call TEXT,
    PRIMARY KEY (accession, infotable_sk)
);

CREATE INDEX sec_13f_holdings_cusip_idx ON public.sec_13f_holdings (cusip);

CREATE TABLE public.company_cusips (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    cusip TEXT,
    source TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT company_cusips_source_check CHECK (source IN (
        'CUSIP_OFFICIAL', 'CUSIP_FROM_NAME_MATCH', 'CUSIP_SOURCES_DISAGREE', 'CUSIP_AMBIGUOUS', 'CUSIP_NOT_FOUND'
    ))
);

CREATE INDEX company_cusips_company_observed_idx ON public.company_cusips (company_id, observed_at);

CREATE TRIGGER sec_13f_datasets_append_only
    BEFORE UPDATE OR DELETE ON public.sec_13f_datasets
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER sec_13f_filings_append_only
    BEFORE UPDATE OR DELETE ON public.sec_13f_filings
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER sec_13f_holdings_append_only
    BEFORE UPDATE OR DELETE ON public.sec_13f_holdings
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER company_cusips_append_only
    BEFORE UPDATE OR DELETE ON public.company_cusips
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();

COMMIT;
