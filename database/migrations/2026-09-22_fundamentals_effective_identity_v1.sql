-- Add source identity metadata to the existing effective projection.
-- The view definition is captured before replacement so the selection policy,
-- row cardinality, and existing column order remain unchanged.

BEGIN;

DO $$
DECLARE
    previous_view_definition text;
    existing_column_count integer;
BEGIN
    IF to_regclass('public.fundamentals_effective_current') IS NULL THEN
        RAISE EXCEPTION 'fundamentals_effective_current view is not deployed';
    END IF;

    SELECT count(*)
      INTO existing_column_count
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'fundamentals_effective_current';

    IF existing_column_count <> 35 THEN
        RAISE EXCEPTION
            'unexpected fundamentals_effective_current column count: expected 35, got %',
            existing_column_count;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'fundamentals_effective_current'
           AND column_name IN ('source_record_id', 'source_identity_type')
    ) THEN
        RAISE EXCEPTION
            'fundamentals_effective_current already contains identity columns';
    END IF;

    SELECT regexp_replace(
               pg_get_viewdef('public.fundamentals_effective_current'::regclass, true),
               '[[:space:];]+$',
               ''
           )
      INTO previous_view_definition;

    IF previous_view_definition IS NULL THEN
        RAISE EXCEPTION 'fundamentals_effective_current view is not deployed';
    END IF;

    EXECUTE
        'CREATE OR REPLACE VIEW fundamentals_effective_current AS '
        || 'SELECT effective.*, '
        || 'raw_identity.source_record_id AS source_record_id, '
        || 'CASE '
        || 'WHEN effective.source = ''SEC'' '
        || ' AND effective.source_variant = ''sec.company_facts'' '
        || ' AND raw_identity.source_record_id IS NOT NULL '
        || ' THEN ''SEC_ACCESSION'' '
        || 'WHEN effective.source = ''YAHOO'' '
        || ' AND raw_identity.source_record_id IS NOT NULL '
        || ' THEN ''YAHOO_SOURCE_RECORD_ID'' '
        || 'WHEN raw_identity.source_record_id IS NOT NULL '
        || ' THEN ''SOURCE_RECORD_ID'' '
        || 'ELSE NULL END AS source_identity_type '
        || 'FROM (' || previous_view_definition || ') AS effective '
        || 'LEFT JOIN fundamentals_raw AS raw_identity '
        || ' ON raw_identity.id = effective.raw_id';
END
$$;

COMMIT;
