-- Experience Store (spec 2026-10-04-experience-store-v1 §3): immutable
-- snapshots of what the system said. Outcomes are derived on read and never
-- stored. Additive only; both tables append-only.

BEGIN;

CREATE TABLE public.experience_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of TIMESTAMPTZ NOT NULL,
    label TEXT,
    code_commit TEXT,
    market JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public.experience_records (
    snapshot_id BIGINT NOT NULL REFERENCES public.experience_snapshots (id) ON DELETE RESTRICT,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    ticker TEXT NOT NULL,
    composite_score NUMERIC(6, 2),
    verdict TEXT,
    letters_passed INTEGER,
    data_status TEXT,
    entry_bar_date DATE,
    entry_close NUMERIC(38, 10),
    payload JSONB NOT NULL,
    PRIMARY KEY (snapshot_id, company_id)
);

CREATE TRIGGER experience_snapshots_append_only
    BEFORE UPDATE OR DELETE ON public.experience_snapshots
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER experience_records_append_only
    BEFORE UPDATE OR DELETE ON public.experience_records
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();

COMMIT;
