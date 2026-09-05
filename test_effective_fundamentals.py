"""Contract tests for contextual diagnostics; no database or provider calls."""

import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal

from database.normalized_fundamentals import NormalizedObservation
from database.effective_fundamentals import compare_observations


def observation(source, value="100", **changes):
    sec = source == "SEC"
    fields = dict(
        id=101 if sec else 202, company_id=1, metric="EPS_DILUTED",
        source=source,
        source_variant="sec.company_facts" if sec else "yahoo.fundamentals_timeseries",
        observation_kind="REPORTED", value=Decimal(value), unit="USD/shares",
        currency="USD", source_period_start=date(2025, 3, 30) if sec else None,
        source_period_end=date(2025, 6, 28), canonical_period_end=date(2025, 6, 28),
        series_date=date(2025, 6, 28), fiscal_year=2025, fiscal_quarter=3,
        filed_date=date(2025, 8, 1) if sec else None, source_available_at=None,
        observed_at=datetime(2026, 9, 4, tzinfo=timezone.utc), raw_id=1 if sec else 2,
        normalizer_version="sec-normalized-v2" if sec else "yahoo-normalized-v2",
        intrinsic_quality_status="OK", intrinsic_quality_reasons=(),
        selection_eligibility="ELIGIBLE", alignment_method="EXACT", alignment_days=0,
        alignment_reference_id=None,
        source_metric_name="EarningsPerShareDiluted" if sec else "quarterlyDilutedEPS",
        source_unit="USD/shares", source_scale_factor=Decimal("1"),
    )
    fields.update(changes)
    return NormalizedObservation(**fields)


class ComparisonTests(unittest.TestCase):
    def assert_incompatible(self, sec, yahoo, reason):
        result = compare_observations(sec, yahoo)
        self.assertIsNone(result.comparison_diff_pct)
        self.assertIn(reason, result.comparison_reasons)
        self.assertEqual(result.comparison_status, "REVIEW_REQUIRED")
        self.assertEqual(result.comparison_rules_version, "sec-yahoo-comparison-v1")
        return result

    def test_exact_percentage_boundaries_through_real_comparator(self):
        # Denominator is exactly 100; input differences are percentages already.
        for yahoo_value, pct, status in (
            ("100", 0.0, "MINOR_DIFFERENCE"),
            ("99.9", 0.1, "MINOR_DIFFERENCE"),
            ("99.899999", 0.100001, "DISCREPANCY_RECORDED"),
            ("99.000001", 0.999999, "DISCREPANCY_RECORDED"),
            ("99", 1.0, "REVIEW_REQUIRED"),
            ("95", 5.0, "REVIEW_REQUIRED"),
            ("94.999999", 5.000001, "REVIEW_REQUIRED_HIGH"),
        ):
            with self.subTest(yahoo_value=yahoo_value):
                result = compare_observations(observation("SEC"), observation("YAHOO", yahoo_value))
                self.assertEqual(result.comparison_diff_pct, pct)
                self.assertEqual(result.comparison_status, status)

    def test_both_zero(self):
        result = compare_observations(observation("SEC", "0"), observation("YAHOO", "0"))
        self.assertEqual(result.comparison_diff_pct, 0.0)
        self.assertEqual(result.comparison_status, "MINOR_DIFFERENCE")

    def test_one_zero_uses_contractual_denominator(self):
        result = compare_observations(observation("SEC", "0"), observation("YAHOO", "1"))
        self.assertEqual(result.comparison_diff_pct, 100.0)
        self.assertEqual(result.comparison_status, "REVIEW_REQUIRED_HIGH")

    def test_denominator_is_maximum_absolute_value_and_symmetric(self):
        for first, second in (("100", "125"), ("125", "100"), ("-100", "-125")):
            with self.subTest(first=first):
                result = compare_observations(observation("SEC", first), observation("YAHOO", second))
                self.assertEqual(result.comparison_diff_pct, 20.0)

    def test_basic_compares_only_to_proven_basic(self):
        sec = observation("SEC", metric="EPS_BASIC", source_metric_name="EarningsPerShareBasic")
        yahoo = observation("YAHOO", metric="EPS_BASIC", source_variant="yfinance.quarterly_income_stmt", source_metric_name="Basic EPS")
        self.assertEqual(compare_observations(sec, yahoo).comparison_diff_pct, 0.0)
        self.assert_incompatible(sec, observation("YAHOO"), "METRIC_MISMATCH")
        self.assert_incompatible(observation("SEC"), yahoo, "METRIC_MISMATCH")

    def test_unspecified_never_compares_and_legacy_unknown_is_not_inferred(self):
        yahoo = observation("YAHOO", metric="EPS_UNSPECIFIED", source_variant="yfinance.quarterly_income_stmt", source_metric_name="legacy.unknown", intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        for metric, tag in (("EPS_DILUTED", "EarningsPerShareDiluted"), ("EPS_BASIC", "EarningsPerShareBasic")):
            self.assert_incompatible(observation("SEC", metric=metric, source_metric_name=tag), yahoo, "EPS_SEMANTICS_UNSPECIFIED")

    def test_eps_alias_contradictions_and_unknown_tags_are_rejected(self):
        for alias in ("Basic EPS", "legacy.unknown", "unprovenDilutedName"):
            with self.subTest(alias=alias):
                self.assert_incompatible(observation("SEC"), observation("YAHOO", source_metric_name=alias), "YAHOO_EPS_SEMANTICS_UNPROVEN")

    def test_sec_tag_must_prove_eps_semantics(self):
        self.assert_incompatible(observation("SEC", source_metric_name="EarningsPerShareBasic"), observation("YAHOO"), "SEC_EPS_SEMANTICS_UNPROVEN")

    def test_dataset_cannot_borrow_another_provider_alias(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_variant="yfinance.quarterly_income_stmt"), "YAHOO_EPS_SEMANTICS_UNPROVEN")

    def test_v1_and_mixed_versions_are_rejected(self):
        for sv, yv in (("sec-normalized-v1", "yahoo-normalized-v2"), ("sec-normalized-v2", "yahoo-normalized-v1"), ("sec-normalized-v1", "yahoo-normalized-v1")):
            self.assert_incompatible(observation("SEC", normalizer_version=sv), observation("YAHOO", normalizer_version=yv), "NORMALIZER_VERSION_MISMATCH")

    def test_roles_and_company_must_match(self):
        self.assert_incompatible(observation("YAHOO"), observation("SEC"), "SOURCE_ROLE_MISMATCH")
        self.assert_incompatible(observation("SEC"), observation("YAHOO", company_id=2), "COMPANY_MISMATCH")

    def test_metric_and_normalized_unit_mismatch(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", metric="REVENUE", unit="USD", source_unit="USD", source_metric_name="quarterlyTotalRevenue"), "UNIT_MISMATCH")

    def test_source_units_must_agree_with_normalized_representation(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_unit="cents/shares"), "SOURCE_UNIT_INCOMPATIBLE")

    def test_scale_mismatch_is_not_silently_converted(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_scale_factor=Decimal("1000")), "SCALE_MISMATCH")

    def test_equal_documented_factors_need_no_conversion(self):
        sec = observation("SEC", source_scale_factor=Decimal("1000"))
        yahoo = observation("YAHOO", source_scale_factor=Decimal("1000"))
        self.assertEqual(compare_observations(sec, yahoo).comparison_diff_pct, 0.0)

    def test_infinite_scale_is_rejected_even_when_dataclass_accepts_it(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_scale_factor=Decimal("Infinity")), "INVALID_SCALE")

    def test_conflicting_explicit_period_starts_are_not_compared(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_period_start=date(2025, 1, 1)), "PERIOD_START_INCOMPATIBLE")

    def test_currency_mismatch_or_missing_monetary_currency(self):
        for currency in ("EUR", None):
            self.assert_incompatible(observation("SEC"), observation("YAHOO", currency=currency), "CURRENCY_INCOMPATIBLE")

    def test_fiscal_identity_must_exist_and_match(self):
        for fields in ({"fiscal_year": 2026}, {"fiscal_quarter": 4}, {"fiscal_year": None}, {"fiscal_quarter": None}):
            self.assert_incompatible(observation("SEC"), observation("YAHOO", **fields), "FISCAL_IDENTITY_INCOMPATIBLE")

    def test_canonical_period_mismatch_is_not_fixed_by_proximity(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", canonical_period_end=date(2025, 6, 27), alignment_days=1, alignment_method="NEAREST_35D"), "CANONICAL_PERIOD_INCOMPATIBLE")

    def test_resolved_alignment_preserves_distinct_source_dates(self):
        yahoo = observation("YAHOO", source_period_end=date(2025, 6, 30), series_date=date(2025, 6, 30), alignment_method="NEAREST_35D", alignment_days=2, alignment_reference_id=101)
        self.assertEqual(compare_observations(observation("SEC"), yahoo).comparison_diff_pct, 0.0)

    def test_unresolved_identity_is_not_numerically_compared(self):
        yahoo = observation("YAHOO", canonical_period_end=None, fiscal_year=None, fiscal_quarter=None, alignment_days=None, alignment_method="UNRESOLVED", intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        self.assert_incompatible(observation("SEC"), yahoo, "CANONICAL_PERIOD_INCOMPATIBLE")

    def test_material_sign_conflict(self):
        self.assert_incompatible(observation("SEC", "1"), observation("YAHOO", "-1"), "SIGN_INCOMPATIBLE")

    def test_nonfinite_values_do_not_enter_arithmetic(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            self.assert_incompatible(observation("SEC", value), observation("YAHOO"), "NONFINITE_VALUE")

    def test_diagnostic_is_frozen_and_references_sec_counterpart(self):
        result = compare_observations(observation("SEC"), observation("YAHOO"))
        self.assertEqual(result.comparison_reference_id, 101)
        self.assertEqual(result.comparison_rules_version, "sec-yahoo-comparison-v1")
        with self.assertRaises(FrozenInstanceError):
            result.comparison_status = "NOT_COMPARED"
        self.assertIsNone(compare_observations(observation("SEC", id=None), observation("YAHOO")).comparison_reference_id)

    def test_reasons_are_deterministic_and_inputs_unchanged_for_all_paths(self):
        sec = observation("SEC")
        for yahoo in (observation("YAHOO"), observation("YAHOO", "90"), observation("YAHOO", currency="EUR", fiscal_year=2026, source_unit="cents/shares")):
            before = deepcopy((asdict(sec), asdict(yahoo)))
            first = compare_observations(sec, yahoo)
            self.assertEqual(first, compare_observations(sec, yahoo))
            self.assertEqual((asdict(sec), asdict(yahoo)), before)
            self.assertIsInstance(first.comparison_reasons, tuple)


if __name__ == "__main__":
    unittest.main()
