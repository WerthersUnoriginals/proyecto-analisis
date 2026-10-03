-- Every distinct EDGAR acceptance time observed for a filing (spec
-- 2026-10-03-new-highs-n-v1 §14). SEC submissions sometimes serve values
-- shifted by the New York UTC offset; each value is kept, none is inferred.
-- Additive only; append-only.

BEGIN;

CREATE TABLE public.sec_filing_acceptance_observations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL,
    accession TEXT NOT NULL,
    acceptance_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sec_filing_acceptance_observations_filing_fk FOREIGN KEY (company_id, accession)
        REFERENCES public.sec_filings (company_id, accession) ON DELETE RESTRICT
);

-- One row per distinct value: the first observation of each value is kept.
CREATE UNIQUE INDEX sec_filing_acceptance_observations_identity_unique
    ON public.sec_filing_acceptance_observations (company_id, accession, acceptance_at);

-- The values already stored are the first observations of their filings.
INSERT INTO public.sec_filing_acceptance_observations (company_id, accession, acceptance_at, observed_at, run_id)
SELECT company_id, accession, acceptance_at, observed_at, run_id
FROM public.sec_filings
WHERE acceptance_at IS NOT NULL;

CREATE TRIGGER sec_filing_acceptance_observations_append_only
    BEFORE UPDATE OR DELETE ON public.sec_filing_acceptance_observations
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();

COMMIT;
