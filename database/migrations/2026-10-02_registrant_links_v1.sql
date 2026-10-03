-- Successor/predecessor registrant links found from official EDGAR evidence
-- (succession filing 8-K12B/8-K12G3 plus jointly filed periodic reports).
-- Append-only: a later reassessment adds a row, it never edits one.

BEGIN;

CREATE TABLE public.registrant_links (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    successor_cik TEXT NOT NULL,
    predecessor_cik TEXT,
    succession_form TEXT NOT NULL,
    succession_accession TEXT NOT NULL,
    succession_date DATE NOT NULL,
    status TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT registrant_links_status_check CHECK (
        (status = 'LINKED' AND predecessor_cik IS NOT NULL)
        OR (status = 'PREDECESSOR_NOT_FOUND' AND predecessor_cik IS NULL)
    ),
    CONSTRAINT registrant_links_cik_check CHECK (
        successor_cik ~ '^[0-9]{10}$' AND (predecessor_cik IS NULL OR predecessor_cik ~ '^[0-9]{10}$')
    )
);

CREATE UNIQUE INDEX registrant_links_identity_unique
    ON public.registrant_links (company_id, succession_accession, predecessor_cik, status)
    NULLS NOT DISTINCT;

CREATE TRIGGER registrant_links_append_only
    BEFORE UPDATE OR DELETE ON public.registrant_links
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();

COMMIT;
