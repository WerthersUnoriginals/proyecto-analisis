-- Remove sec_filings_acceptance_check (see _v2). Raw evidence is stored
-- verbatim: real EDGAR records violate any fixed acceptance/filing-date bound
-- (e.g. AAPL 9999999997-03-008286, form NO ACT, filed 2003-03-03, accepted
-- 2003-03-12). Date coherence is checked where availability is used, and
-- only for periodic forms.

BEGIN;

ALTER TABLE public.sec_filings DROP CONSTRAINT sec_filings_acceptance_check;

COMMIT;
