-- Fix ingestion_runs_status_check from 2026-10-02_evidence_v3.sql: a regular
-- expression on NULL evaluates to NULL and a CHECK that is NULL passes, so a
-- FAILED run without error_code was accepted. Found by the opt-in PostgreSQL
-- integration test test_failed_run_requires_symbolic_error_code.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.ingestion_runs WHERE status = 'FAILED' AND error_code IS NULL) THEN
        RAISE EXCEPTION 'existing FAILED runs without error_code; resolve before tightening the check';
    END IF;
END $$;

ALTER TABLE public.ingestion_runs DROP CONSTRAINT ingestion_runs_status_check;

ALTER TABLE public.ingestion_runs ADD CONSTRAINT ingestion_runs_status_check CHECK (
    (status = 'SUCCESS' AND error_code IS NULL)
    OR (status = 'FAILED' AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,63}$')
);

COMMIT;
