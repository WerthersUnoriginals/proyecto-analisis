-- CAN SLIM+ — observaciones normalizadas por fuente, inmutables por versión.
-- Este artefacto es aditivo: no modifica las tablas legacy ni crea la capa efectiva.

BEGIN;

CREATE TABLE IF NOT EXISTS fundamentals_normalized (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    metric TEXT NOT NULL,
    source TEXT NOT NULL,
    source_variant TEXT NOT NULL,
    observation_kind TEXT NOT NULL,
    value NUMERIC(30, 8) NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT,
    source_period_start DATE,
    source_period_end DATE NOT NULL,
    canonical_period_end DATE,
    series_date DATE NOT NULL,
    fiscal_year INTEGER,
    fiscal_quarter SMALLINT,
    filed_date DATE,
    source_available_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    raw_id BIGINT NOT NULL REFERENCES fundamentals_raw(id) ON DELETE RESTRICT,
    normalizer_version TEXT NOT NULL,
    intrinsic_quality_status TEXT NOT NULL,
    intrinsic_quality_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    selection_eligibility TEXT NOT NULL,
    alignment_method TEXT NOT NULL,
    alignment_days SMALLINT,
    alignment_reference_id BIGINT REFERENCES fundamentals_normalized(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fundamentals_normalized_raw_version_unique
        UNIQUE (raw_id, normalizer_version),

    CONSTRAINT fundamentals_normalized_metric_check
        CHECK (metric IN ('EPS_DILUTED', 'REVENUE', 'NET_INCOME', 'DILUTED_SHARES')),

    CONSTRAINT fundamentals_normalized_unit_check
        CHECK (
            (metric = 'EPS_DILUTED' AND unit = 'USD/shares')
            OR (metric IN ('REVENUE', 'NET_INCOME') AND unit = 'USD')
            OR (metric = 'DILUTED_SHARES' AND unit = 'shares')
        ),

    CONSTRAINT fundamentals_normalized_source_check
        CHECK (source IN ('SEC', 'YAHOO', 'DERIVED')),

    CONSTRAINT fundamentals_normalized_variant_check
        CHECK (
            (source = 'SEC' AND source_variant LIKE 'sec.%')
            OR (source = 'YAHOO' AND (source_variant LIKE 'yahoo.%' OR source_variant LIKE 'yfinance.%'))
            OR (source = 'DERIVED' AND source_variant LIKE 'derived.%')
        ),

    CONSTRAINT fundamentals_normalized_observation_kind_check
        CHECK (observation_kind IN ('REPORTED', 'DERIVED')),

    CONSTRAINT fundamentals_normalized_source_kind_filing_check
        CHECK (
            (source = 'SEC' AND observation_kind = 'REPORTED'
                AND (selection_eligibility = 'INELIGIBLE' OR filed_date IS NOT NULL))
            OR (source = 'YAHOO' AND observation_kind = 'REPORTED' AND filed_date IS NULL)
            OR (source = 'DERIVED' AND observation_kind = 'DERIVED'
                AND selection_eligibility = 'INELIGIBLE')
        ),

    CONSTRAINT fundamentals_normalized_fiscal_quarter_check
        CHECK (fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4),

    CONSTRAINT fundamentals_normalized_period_check
        CHECK (source_period_start IS NULL OR source_period_start <= source_period_end),

    CONSTRAINT fundamentals_normalized_series_date_check
        CHECK (series_date = source_period_end),

    CONSTRAINT fundamentals_normalized_quality_status_check
        CHECK (intrinsic_quality_status IN ('OK', 'REVIEW_REQUIRED', 'REVIEW_REQUIRED_HIGH', 'REJECTED')),

    CONSTRAINT fundamentals_normalized_selection_eligibility_check
        CHECK (selection_eligibility IN ('ELIGIBLE', 'INELIGIBLE')),

    CONSTRAINT fundamentals_normalized_review_ineligible_check
        CHECK (
            intrinsic_quality_status = 'OK'
            OR (intrinsic_quality_status = 'REVIEW_REQUIRED' AND selection_eligibility = 'INELIGIBLE')
            OR (intrinsic_quality_status = 'REVIEW_REQUIRED_HIGH' AND selection_eligibility = 'INELIGIBLE')
            OR (intrinsic_quality_status = 'REJECTED' AND selection_eligibility = 'INELIGIBLE')
        ),

    CONSTRAINT fundamentals_normalized_alignment_method_check
        CHECK (alignment_method IN ('EXACT', 'FISCAL_METADATA', 'SEC_CALENDAR', 'NEAREST_35D', 'SOURCE_ONLY', 'UNRESOLVED')),

    CONSTRAINT fundamentals_normalized_alignment_days_check
        CHECK (
            (canonical_period_end IS NULL AND alignment_days IS NULL)
            OR (
                canonical_period_end IS NOT NULL
                AND alignment_days IS NOT NULL
                AND alignment_days = source_period_end - canonical_period_end
            )
        ),

    CONSTRAINT fundamentals_normalized_exact_alignment_check
        CHECK (
            (alignment_method = 'EXACT'
                AND canonical_period_end IS NOT NULL
                AND alignment_days = 0)
            OR alignment_method IN ('FISCAL_METADATA', 'SEC_CALENDAR', 'NEAREST_35D', 'SOURCE_ONLY', 'UNRESOLVED')
        ),

    CONSTRAINT fundamentals_normalized_unresolved_alignment_check
        CHECK (
            (alignment_method = 'UNRESOLVED'
                AND canonical_period_end IS NULL
                AND intrinsic_quality_status IN ('REVIEW_REQUIRED', 'REVIEW_REQUIRED_HIGH')
                AND selection_eligibility = 'INELIGIBLE')
            OR alignment_method IN ('EXACT', 'FISCAL_METADATA', 'SEC_CALENDAR', 'NEAREST_35D', 'SOURCE_ONLY')
        )
);

CREATE INDEX fundamentals_normalized_source_period_idx
    ON fundamentals_normalized (company_id, metric, source, source_variant, source_period_end);

CREATE INDEX fundamentals_normalized_fiscal_idx
    ON fundamentals_normalized (company_id, metric, fiscal_year, fiscal_quarter);

CREATE INDEX fundamentals_normalized_observed_idx
    ON fundamentals_normalized (company_id, metric, observed_at);

COMMIT;
