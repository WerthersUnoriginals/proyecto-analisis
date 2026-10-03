-- L evidence (spec 2026-10-03-leader-laggard-l-v1 §3): the literal S&P 500
-- holdings of the SPY ETF (State Street) and the literal SEC SIC code of each
-- company. Additive except the provider check of ingestion_runs, which gains
-- SSGA. All new tables are append-only.

BEGIN;

ALTER TABLE public.ingestion_runs DROP CONSTRAINT ingestion_runs_provider_check;
ALTER TABLE public.ingestion_runs ADD CONSTRAINT ingestion_runs_provider_check
    CHECK (provider IN ('SEC', 'YAHOO_FINANCE', 'SSGA'));

CREATE TABLE public.universe_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    universe TEXT NOT NULL,
    holdings_as_of DATE NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One snapshot per holdings date: a later download of the same file is not stored again.
CREATE UNIQUE INDEX universe_snapshots_identity_unique
    ON public.universe_snapshots (universe, holdings_as_of);

CREATE TABLE public.universe_members (
    snapshot_id BIGINT NOT NULL REFERENCES public.universe_snapshots (id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    source_ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    identifier TEXT,
    sedol TEXT,
    weight NUMERIC(20, 8),
    sector TEXT,
    shares_held NUMERIC(38, 4),
    currency TEXT,
    PRIMARY KEY (snapshot_id, position)
);

CREATE TABLE public.company_profiles (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    cik TEXT NOT NULL,
    sic TEXT,
    sic_description TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT company_profiles_cik_check CHECK (cik ~ '^[0-9]{10}$')
);

CREATE INDEX company_profiles_company_observed_idx ON public.company_profiles (company_id, observed_at);

CREATE TRIGGER universe_snapshots_append_only
    BEFORE UPDATE OR DELETE ON public.universe_snapshots
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER universe_members_append_only
    BEFORE UPDATE OR DELETE ON public.universe_members
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER company_profiles_append_only
    BEFORE UPDATE OR DELETE ON public.company_profiles
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();

COMMIT;
