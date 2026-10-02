-- Correct sec_filings_acceptance_check from 2026-10-02_evidence_v3.sql.
-- EDGAR assigns filings accepted after hours to the next business day, so
-- acceptance can precede the filing date by several days (weekends, holidays):
-- e.g. AAPL 0000912057-02-029692, accepted Friday 2002-08-02 22:59:57Z, filed
-- Monday 2002-08-05. The real invariant is the opposite bound: acceptance is
-- never later than the filing date, allowing one day because late same-day
-- forms (until 22:00 New York) fall on the next UTC date.

BEGIN;

ALTER TABLE public.sec_filings DROP CONSTRAINT sec_filings_acceptance_check;

ALTER TABLE public.sec_filings ADD CONSTRAINT sec_filings_acceptance_check CHECK (
    acceptance_at IS NULL
    OR (acceptance_at AT TIME ZONE 'UTC')::date <= filing_date + 1
);

COMMIT;
