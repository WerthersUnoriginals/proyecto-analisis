-- Record where each verbatim SEC fact was read: the Company Facts API or the
-- filing's own XBRL instance (used while the API lags behind EDGAR). Existing
-- rows came from the API. Adding a column with a constant default does not
-- rewrite or update rows, so the append-only trigger is not involved.

BEGIN;

ALTER TABLE public.sec_companyfacts_raw
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'sec.company_facts';

ALTER TABLE public.sec_companyfacts_raw
    ADD CONSTRAINT sec_companyfacts_raw_origin_check
    CHECK (origin IN ('sec.company_facts', 'sec.xbrl_instance'));

COMMIT;
