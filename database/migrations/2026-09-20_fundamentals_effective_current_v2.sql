-- Task 9A: proyección current, no materializada, equivalente al selector Python.
CREATE VIEW fundamentals_effective_current AS
WITH eligible AS (
    SELECT normalized.*
    FROM fundamentals_normalized AS normalized
    WHERE normalized.source IN ('SEC', 'YAHOO')
      AND (
          (
              normalized.source = 'SEC'
              AND normalized.source_variant = 'sec.company_facts'
              AND normalized.normalizer_version = 'sec-normalized-v2'
          )
          OR (
              normalized.source = 'YAHOO'
              AND normalized.source_variant IN (
                  'yahoo.fundamentals_timeseries',
                  'yfinance.quarterly_income_stmt'
              )
              AND normalized.normalizer_version = 'yahoo-normalized-v2'
          )
      )
      AND normalized.observation_kind = 'REPORTED'
      AND normalized.selection_eligibility = 'ELIGIBLE'
      AND normalized.metric IN ('EPS_DILUTED', 'REVENUE', 'NET_INCOME', 'DILUTED_SHARES')
      AND NOT (normalized.source = 'YAHOO' AND normalized.metric = 'DILUTED_SHARES')
      AND normalized.source_unit = normalized.unit
      AND normalized.source_scale_factor > 0
      AND normalized.source_scale_factor::text NOT IN ('NaN', 'Infinity', '-Infinity')
      AND (normalized.unit = 'shares' OR normalized.currency IS NOT NULL)
      AND normalized.fiscal_year IS NOT NULL
      AND normalized.fiscal_quarter IS NOT NULL
      AND normalized.canonical_period_end IS NOT NULL
      AND normalized.alignment_method <> 'UNRESOLVED'
      AND normalized.value::text NOT IN ('NaN', 'Infinity', '-Infinity')
      AND (
          normalized.metric <> 'EPS_DILUTED'
          OR (
              normalized.source_variant = 'sec.company_facts'
              AND normalized.source_metric_name = 'EarningsPerShareDiluted'
          )
          OR (
              normalized.source_variant = 'yahoo.fundamentals_timeseries'
              AND normalized.source_metric_name = 'quarterlyDilutedEPS'
          )
          OR (
              normalized.source_variant = 'yfinance.quarterly_income_stmt'
              AND normalized.source_metric_name IN ('Diluted EPS', 'DilutedEPS')
          )
      )
),
annotated AS (
    SELECT
        candidate.*,
        EXISTS (
            SELECT 1
            FROM eligible AS other
            WHERE other.id <> candidate.id
              AND other.company_id = candidate.company_id
              AND other.metric = candidate.metric
              AND other.source_variant = candidate.source_variant
              AND other.source_period_end = candidate.source_period_end
              AND (
                  other.unit IS DISTINCT FROM candidate.unit
                  OR other.source_scale_factor IS DISTINCT FROM candidate.source_scale_factor
                  OR other.currency IS DISTINCT FROM candidate.currency
                  OR other.fiscal_year IS DISTINCT FROM candidate.fiscal_year
                  OR other.fiscal_quarter IS DISTINCT FROM candidate.fiscal_quarter
                  OR other.canonical_period_end IS DISTINCT FROM candidate.canonical_period_end
                  OR (
                      other.source_period_start IS NOT NULL
                      AND candidate.source_period_start IS NOT NULL
                      AND other.source_period_start <> candidate.source_period_start
                  )
                  OR (other.value < 0 AND candidate.value > 0)
                  OR (candidate.value < 0 AND other.value > 0)
              )
        ) AS group_conflict,
        COUNT(*) OVER (
            PARTITION BY candidate.company_id, candidate.metric,
                         candidate.source_variant, candidate.source_period_end
        ) AS group_size,
        ROW_NUMBER() OVER (
            PARTITION BY candidate.company_id, candidate.metric,
                         candidate.source_variant, candidate.source_period_end
            ORDER BY candidate.filed_date DESC NULLS LAST, candidate.raw_id DESC
        ) AS source_rank
    FROM eligible AS candidate
),
classified AS (
    SELECT
        annotated.*,
        CASE
            WHEN annotated.group_conflict THEN 'AMBIGUOUS'
            WHEN annotated.source = 'SEC' AND annotated.source_rank = 1 THEN 'RESOLVED'
            WHEN annotated.source = 'SEC' THEN 'SUPERSEDED'
            WHEN annotated.group_size = 1 THEN 'RESOLVED'
            ELSE 'AMBIGUOUS'
        END AS resolution_state
    FROM annotated
),
resolved AS (
    SELECT *
    FROM classified
    WHERE resolution_state = 'RESOLVED'
),
ambiguous AS (
    SELECT *
    FROM classified
    WHERE resolution_state = 'AMBIGUOUS'
),
sec_resolved AS (
    SELECT *
    FROM resolved
    WHERE source = 'SEC'
),
yahoo_timeseries AS (
    SELECT *
    FROM resolved
    WHERE source_variant = 'yahoo.fundamentals_timeseries'
),
yfinance AS (
    SELECT *
    FROM resolved
    WHERE source_variant = 'yfinance.quarterly_income_stmt'
),
yahoo_candidates AS (
    SELECT *
    FROM yahoo_timeseries

    UNION ALL

    SELECT yfinance.*
    FROM yfinance
    WHERE NOT EXISTS (
        SELECT 1
        FROM classified AS timeseries_anchor
        WHERE timeseries_anchor.source_variant = 'yahoo.fundamentals_timeseries'
          AND timeseries_anchor.resolution_state IN ('RESOLVED', 'AMBIGUOUS')
          AND timeseries_anchor.company_id = yfinance.company_id
          AND timeseries_anchor.metric = yfinance.metric
          AND ABS(timeseries_anchor.series_date - yfinance.series_date) <= 35
    )
),
yahoo_context AS (
    SELECT
        yahoo.id AS yahoo_id,
        EXISTS (
            SELECT 1
            FROM ambiguous AS blocked_sec
            WHERE blocked_sec.source = 'SEC'
              AND blocked_sec.company_id = yahoo.company_id
              AND blocked_sec.metric = yahoo.metric
              AND ABS(blocked_sec.series_date - yahoo.series_date) <= 35
        ) AS blocked_by_ambiguous_sec,
        EXISTS (
            SELECT 1
            FROM sec_resolved AS nearby_sec
            WHERE nearby_sec.company_id = yahoo.company_id
              AND nearby_sec.metric = yahoo.metric
              AND ABS(nearby_sec.series_date - yahoo.series_date) <= 35
        ) AS has_nearby_sec
    FROM yahoo_candidates AS yahoo
),
yahoo_sec_pairs AS (
    SELECT
        yahoo.id AS yahoo_id,
        sec.id AS sec_id,
        ABS(sec.series_date - yahoo.series_date) AS distance_days
    FROM yahoo_candidates AS yahoo
    JOIN sec_resolved AS sec
      ON sec.company_id = yahoo.company_id
     AND sec.metric = yahoo.metric
     AND ABS(sec.series_date - yahoo.series_date) <= 35
     AND sec.unit = yahoo.unit
     AND sec.source_scale_factor = yahoo.source_scale_factor
     AND sec.currency IS NOT DISTINCT FROM yahoo.currency
     AND sec.fiscal_year = yahoo.fiscal_year
     AND sec.fiscal_quarter = yahoo.fiscal_quarter
     AND sec.canonical_period_end = yahoo.canonical_period_end
     AND NOT (
         sec.source_period_start IS NOT NULL
         AND yahoo.source_period_start IS NOT NULL
         AND sec.source_period_start <> yahoo.source_period_start
     )
     AND NOT (
         (sec.value < 0 AND yahoo.value > 0)
         OR (yahoo.value < 0 AND sec.value > 0)
     )
),
yahoo_sec_ranked AS (
    SELECT
        pair.*,
        MIN(pair.distance_days) OVER (PARTITION BY pair.yahoo_id) AS minimum_distance
    FROM yahoo_sec_pairs AS pair
),
yahoo_sec_choice AS (
    SELECT
        ranked.yahoo_id,
        CASE
            WHEN COUNT(*) FILTER (
                WHERE ranked.distance_days = ranked.minimum_distance
            ) = 1
            THEN MIN(ranked.sec_id) FILTER (
                WHERE ranked.distance_days = ranked.minimum_distance
            )
        END AS chosen_sec_id
    FROM yahoo_sec_ranked AS ranked
    GROUP BY ranked.yahoo_id
),
counterpart_mappings AS (
    SELECT
        choice.yahoo_id,
        choice.chosen_sec_id AS sec_id
    FROM yahoo_sec_choice AS choice
    JOIN yahoo_context AS context ON context.yahoo_id = choice.yahoo_id
    WHERE NOT context.blocked_by_ambiguous_sec
      AND choice.chosen_sec_id IS NOT NULL
),
sec_yahoo_distances AS (
    SELECT
        mapping.sec_id,
        mapping.yahoo_id,
        ABS(sec.series_date - yahoo.series_date) AS distance_days
    FROM counterpart_mappings AS mapping
    JOIN sec_resolved AS sec ON sec.id = mapping.sec_id
    JOIN yahoo_candidates AS yahoo ON yahoo.id = mapping.yahoo_id
),
sec_yahoo_ranked AS (
    SELECT
        distance.*,
        MIN(distance.distance_days) OVER (PARTITION BY distance.sec_id) AS minimum_distance,
        COUNT(*) OVER (PARTITION BY distance.sec_id) AS candidate_count
    FROM sec_yahoo_distances AS distance
),
sec_yahoo_choice AS (
    SELECT
        ranked.sec_id,
        MAX(ranked.candidate_count) AS candidate_count,
        CASE
            WHEN COUNT(*) FILTER (
                WHERE ranked.distance_days = ranked.minimum_distance
            ) = 1
            THEN MIN(ranked.yahoo_id) FILTER (
                WHERE ranked.distance_days = ranked.minimum_distance
            )
        END AS chosen_yahoo_id
    FROM sec_yahoo_ranked AS ranked
    GROUP BY ranked.sec_id
),
sec_with_yahoo AS (
    SELECT
        sec.*,
        choice.candidate_count,
        yahoo.id AS comparison_yahoo_id,
        CASE
            WHEN yahoo.id IS NULL THEN NULL::numeric
            WHEN GREATEST(ABS(sec.value), ABS(yahoo.value)) = 0 THEN 0::numeric
            ELSE ABS(sec.value - yahoo.value)
                 / GREATEST(ABS(sec.value), ABS(yahoo.value)) * 100
        END AS comparison_difference_pct
    FROM sec_resolved AS sec
    LEFT JOIN sec_yahoo_choice AS choice ON choice.sec_id = sec.id
    LEFT JOIN yahoo_candidates AS yahoo ON yahoo.id = choice.chosen_yahoo_id
),
effective_rows AS (
    SELECT
        sec.*,
        CASE
            WHEN sec.comparison_yahoo_id IS NOT NULL THEN 'SEC_REPORTED_PRIORITY'
            ELSE 'SEC_REPORTED_NO_COMPARABLE_YAHOO'
        END AS effective_selection_reason,
        CASE
            WHEN sec.comparison_yahoo_id IS NULL THEN 'NOT_COMPARED'
            WHEN sec.comparison_difference_pct <= 0.1 THEN 'MINOR_DIFFERENCE'
            WHEN sec.comparison_difference_pct < 1 THEN 'DISCREPANCY_RECORDED'
            WHEN sec.comparison_difference_pct <= 5 THEN 'REVIEW_REQUIRED'
            ELSE 'REVIEW_REQUIRED_HIGH'
        END AS effective_comparison_status,
        CASE
            WHEN sec.comparison_yahoo_id IS NOT NULL THEN NULL::text
            WHEN COALESCE(sec.candidate_count, 0) > 0 THEN 'AMBIGUOUS_YAHOO_COUNTERPART'
            ELSE 'NO_COMPARABLE_YAHOO_WITHIN_35D'
        END AS effective_comparison_reason,
        CASE
            WHEN sec.comparison_yahoo_id IS NOT NULL THEN sec.id
        END AS effective_comparison_reference_id
    FROM sec_with_yahoo AS sec

    UNION ALL

    SELECT
        yahoo.*,
        NULL::bigint AS candidate_count,
        NULL::bigint AS comparison_yahoo_id,
        NULL::numeric AS comparison_difference_pct,
        'YAHOO_FALLBACK_NO_SEC_WITHIN_35D' AS effective_selection_reason,
        'NOT_COMPARED' AS effective_comparison_status,
        'NO_COMPARABLE_SEC_WITHIN_35D' AS effective_comparison_reason,
        NULL::bigint AS effective_comparison_reference_id
    FROM yahoo_candidates AS yahoo
    JOIN yahoo_context AS context ON context.yahoo_id = yahoo.id
    WHERE NOT context.blocked_by_ambiguous_sec
      AND NOT context.has_nearby_sec
)
SELECT
    effective.company_id AS company_id,
    effective.metric AS metric,
    effective.fiscal_year AS fiscal_year,
    effective.fiscal_quarter AS fiscal_quarter,
    effective.canonical_period_end AS canonical_period_end,
    effective.series_date AS series_date,
    effective.value AS value,
    effective.unit AS unit,
    effective.currency AS currency,
    effective.source AS source,
    effective.source_variant AS source_variant,
    effective.observation_kind AS observation_kind,
    effective.id AS selected_observation_id,
    effective.raw_id AS raw_id,
    effective.source_metric_name AS source_metric_name,
    effective.source_unit AS source_unit,
    effective.source_scale_factor AS source_scale_factor,
    effective.source_period_start AS source_period_start,
    effective.source_period_end AS source_period_end,
    effective.filed_date AS filed_date,
    effective.source_available_at AS source_available_at,
    effective.observed_at AS observed_at,
    effective.normalizer_version AS normalizer_version,
    effective.intrinsic_quality_status AS intrinsic_quality_status,
    effective.selection_eligibility AS selection_eligibility,
    'c-v2.6-compatible-v1'::text AS selection_policy_version,
    effective.effective_selection_reason AS selection_reason,
    'sec-yahoo-comparison-v1'::text AS comparison_rules_version,
    effective.effective_comparison_status AS comparison_status,
    effective.effective_comparison_reason AS comparison_reason,
    effective.effective_comparison_reference_id AS comparison_reference_id,
    effective.comparison_difference_pct AS comparison_difference_pct,
    effective.alignment_method AS alignment_method,
    effective.alignment_days AS alignment_days,
    effective.alignment_reference_id AS alignment_reference_id
FROM effective_rows AS effective
ORDER BY
    effective.company_id,
    effective.metric,
    effective.series_date,
    effective.source_variant,
    effective.raw_id;
