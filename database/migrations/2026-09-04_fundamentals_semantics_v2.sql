-- Fase 3A / mini-Task 6B: trazabilidad semántica normalizada v2.
-- Deliberadamente sin BEGIN/COMMIT: el orquestador ejecuta DDL, backfill y gates
-- en una sola transacción real.

ALTER TABLE fundamentals_raw
    DROP CONSTRAINT IF EXISTS fundamentals_raw_metric_check;

ALTER TABLE fundamentals_raw
    ADD CONSTRAINT fundamentals_raw_metric_check
    CHECK (metric IN (
        'EPS_DILUTED',
        'EPS_BASIC',
        'REVENUE',
        'NET_INCOME',
        'DILUTED_SHARES'
    ));

ALTER TABLE fundamentals_normalized
    ADD COLUMN IF NOT EXISTS source_metric_name TEXT,
    ADD COLUMN IF NOT EXISTS source_unit TEXT,
    ADD COLUMN IF NOT EXISTS source_scale_factor NUMERIC(30, 12);

ALTER TABLE fundamentals_normalized
    DROP CONSTRAINT IF EXISTS fundamentals_normalized_metric_check,
    DROP CONSTRAINT IF EXISTS fundamentals_normalized_unit_check,
    DROP CONSTRAINT IF EXISTS fundamentals_normalized_v2_source_metric_name_check,
    DROP CONSTRAINT IF EXISTS fundamentals_normalized_v2_source_unit_check,
    DROP CONSTRAINT IF EXISTS fundamentals_normalized_v2_source_scale_check,
    DROP CONSTRAINT IF EXISTS fundamentals_normalized_eps_unspecified_check;

ALTER TABLE fundamentals_normalized
    ADD CONSTRAINT fundamentals_normalized_metric_check
        CHECK (metric IN (
            'EPS_DILUTED',
            'EPS_BASIC',
            'EPS_UNSPECIFIED',
            'REVENUE',
            'NET_INCOME',
            'DILUTED_SHARES'
        )),
    ADD CONSTRAINT fundamentals_normalized_unit_check
        CHECK (
            (metric IN ('EPS_DILUTED', 'EPS_BASIC', 'EPS_UNSPECIFIED')
                AND unit = 'USD/shares')
            OR (metric IN ('REVENUE', 'NET_INCOME') AND unit = 'USD')
            OR (metric = 'DILUTED_SHARES' AND unit = 'shares')
        ),
    ADD CONSTRAINT fundamentals_normalized_v2_source_metric_name_check
        CHECK (
            normalizer_version NOT IN ('sec-normalized-v2', 'yahoo-normalized-v2')
            OR (source_metric_name IS NOT NULL AND BTRIM(source_metric_name) <> '')
        ),
    ADD CONSTRAINT fundamentals_normalized_v2_source_unit_check
        CHECK (
            normalizer_version NOT IN ('sec-normalized-v2', 'yahoo-normalized-v2')
            OR (source_unit IS NOT NULL AND BTRIM(source_unit) <> '')
        ),
    ADD CONSTRAINT fundamentals_normalized_v2_source_scale_check
        CHECK (
            normalizer_version NOT IN ('sec-normalized-v2', 'yahoo-normalized-v2')
            OR source_scale_factor > 0
        ),
    ADD CONSTRAINT fundamentals_normalized_eps_unspecified_check
        CHECK (
            metric <> 'EPS_UNSPECIFIED'
            OR (
                source = 'YAHOO'
                AND normalizer_version = 'yahoo-normalized-v2'
                AND source_metric_name = 'legacy.unknown'
                AND intrinsic_quality_status = 'REVIEW_REQUIRED'
                AND selection_eligibility = 'INELIGIBLE'
            )
        );
