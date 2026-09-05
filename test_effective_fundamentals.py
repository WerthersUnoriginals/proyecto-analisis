"""Contract tests for contextual diagnostics; no database or provider calls."""

import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timedelta, timezone
from itertools import permutations
from decimal import Decimal

from database.normalized_fundamentals import NormalizedObservation
from database.effective_fundamentals import compare_observations, select_effective_observations


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


class SelectionTests(unittest.TestCase):
    def yfinance(self, offset=0, **fields):
        end = date(2025, 6, 28) + timedelta(days=offset)
        fields = dict(dict(id=203, raw_id=3,
            source_variant="yfinance.quarterly_income_stmt",
            source_metric_name="Diluted EPS", source_period_end=end, series_date=end,
            alignment_method="FISCAL_METADATA", alignment_days=offset), **fields)
        return observation("YAHOO", **fields)

    def test_yahoo_priority_same_date_and_inclusive_window_in_both_orders(self):
        ts = observation("YAHOO")
        for offset in (0, -35, 35, -36, 36):
            yf = self.yfinance(offset)
            expected = {202} if abs(offset) <= 35 else {202, 203}
            for order in ((ts, yf), (yf, ts)):
                with self.subTest(offset=offset, order=order[0].id):
                    result = select_effective_observations(order)
                    self.assertEqual({x.observation.id for x in result}, expected)

    def test_sec_priority_same_date_and_inclusive_window_in_both_orders(self):
        sec = observation("SEC")
        for offset in (0, -35, 35, -36, 36):
            yahoo = self.yfinance(offset)
            for order in ((sec, yahoo), (yahoo, sec)):
                with self.subTest(offset=offset):
                    result = select_effective_observations(order)
                    expected = {101} if abs(offset) <= 35 else {101, 203}
                    self.assertEqual({x.observation.id for x in result}, expected)

    def test_yahoo_is_resolved_before_sec_comparison(self):
        sec, ts, yf = observation("SEC"), observation("YAHOO", "95"), self.yfinance(value="90")
        result = select_effective_observations([yf, ts, sec])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].comparison, compare_observations(sec, ts))

    def test_review_and_high_numeric_review_preserve_sec_priority(self):
        for value, status in (("95", "REVIEW_REQUIRED"), ("90", "REVIEW_REQUIRED_HIGH")):
            sec, yahoo = observation("SEC"), observation("YAHOO", value)
            result = select_effective_observations([yahoo, sec])
            self.assertEqual([x.observation for x in result], [sec])
            self.assertEqual(result[0].comparison.comparison_status, status)
            self.assertEqual(result[0].comparison, compare_observations(sec, yahoo))

    def test_fallback_has_explicit_reason_and_no_fabricated_comparison(self):
        yahoo = observation("YAHOO")
        result = select_effective_observations([yahoo])[0]
        self.assertIs(result.observation, yahoo)
        self.assertEqual(result.selection_reason, "YAHOO_FALLBACK_NO_SEC_WITHIN_35D")
        self.assertEqual(result.selection_policy_version, "c-v2.6-compatible-v1")
        self.assertEqual(result.comparison.comparison_status, "NOT_COMPARED")
        self.assertIsNone(result.comparison.comparison_diff_pct)
        self.assertIsNone(result.comparison.comparison_reference_id)

    def test_v1_ineligible_and_unknown_source_variants_are_excluded(self):
        rows = [observation("SEC", normalizer_version="sec-normalized-v1"),
            observation("YAHOO", normalizer_version="yahoo-normalized-v1"),
            observation("SEC", selection_eligibility="INELIGIBLE"),
            observation("YAHOO", source_variant="yahoo.future")]
        self.assertEqual(select_effective_observations(rows), [])

    def test_derived_is_excluded(self):
        row = replace(observation("YAHOO"), source="DERIVED", source_variant="derived.future",
            observation_kind="DERIVED", selection_eligibility="INELIGIBLE")
        self.assertEqual(select_effective_observations([row]), [])

    def test_basic_and_unspecified_cannot_supply_c_diluted(self):
        basic = self.yfinance(metric="EPS_BASIC", source_metric_name="Basic EPS")
        unknown = self.yfinance(metric="EPS_UNSPECIFIED", source_metric_name="legacy.unknown",
            intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        self.assertEqual(select_effective_observations([basic, unknown]), [])
        diluted = observation("YAHOO")
        self.assertEqual([x.observation for x in select_effective_observations([basic, diluted])], [diluted])

    def test_yahoo_shares_excluded_while_sec_shares_remain(self):
        fields = dict(metric="DILUTED_SHARES", unit="shares", source_unit="shares", currency=None,
            source_metric_name="WeightedAverageNumberOfDilutedSharesOutstanding")
        sec, yahoo = observation("SEC", **fields), observation("YAHOO", **fields)
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo])], [sec])

    def test_unresolved_and_missing_fiscal_identity_are_excluded(self):
        unresolved = observation("YAHOO", fiscal_year=None, fiscal_quarter=None,
            canonical_period_end=None, alignment_method="UNRESOLVED", alignment_days=None,
            intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        missing = observation("YAHOO", fiscal_year=None)
        self.assertEqual(select_effective_observations([unresolved, missing]), [])

    def test_sec_latest_filing_then_raw_id_uses_contractual_ranking(self):
        first = observation("SEC")
        later = replace(first, id=102, raw_id=4, filed_date=date(2025, 8, 2), value=Decimal("101"))
        tie = replace(later, id=103, raw_id=5, value=Decimal("102"))
        for order in permutations([first, later, tie]):
            self.assertEqual([x.observation for x in select_effective_observations(order)], [tie])

    def test_material_yahoo_snapshot_tie_is_excluded_without_latest_heuristic(self):
        first = observation("YAHOO")
        later = replace(first, id=204, raw_id=4, value=Decimal("101"),
            observed_at=first.observed_at + timedelta(days=1))
        for order in ((first, later), (later, first)):
            self.assertEqual(select_effective_observations(order), [])

    def test_same_sec_rank_with_conflicting_content_is_excluded(self):
        first = observation("SEC")
        conflict = replace(first, id=105, value=Decimal("101"))
        self.assertEqual(select_effective_observations([first, conflict]), [])

    def test_contradictory_fiscal_identity_in_a_source_period_is_excluded(self):
        first = observation("SEC")
        conflict = replace(first, id=105, raw_id=5, fiscal_year=2026)
        self.assertEqual(select_effective_observations([first, conflict]), [])

    def test_equidistant_sec_counterparts_do_not_invent_yahoo_association(self):
        first = observation("SEC", source_period_start=None)
        second = replace(first, id=102, raw_id=2, source_period_end=date(2025, 6, 30),
            series_date=date(2025, 6, 30), alignment_method="FISCAL_METADATA", alignment_days=2)
        yahoo = self.yfinance(1)
        result = select_effective_observations([first, second, yahoo])
        self.assertEqual({x.observation.id for x in result}, {101, 102})
        self.assertTrue(all(x.comparison.comparison_status == "NOT_COMPARED" for x in result))

    def test_series_date_and_all_input_fields_are_preserved(self):
        yahoo = self.yfinance(36)
        sec = observation("SEC")
        before = deepcopy([asdict(sec), asdict(yahoo)])
        result = select_effective_observations([yahoo, sec])
        self.assertEqual([asdict(sec), asdict(yahoo)], before)
        self.assertEqual(next(x.observation.series_date for x in result if x.observation.source == "YAHOO"), date(2025, 8, 3))
        with self.assertRaises(FrozenInstanceError):
            result[0].selection_reason = "changed"

    def test_as_of_filters_before_ranking_and_includes_exact_boundary(self):
        sec = observation("SEC")
        yahoo = replace(observation("YAHOO"), observed_at=sec.observed_at - timedelta(days=1))
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo], yahoo.observed_at)], [yahoo])
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo], sec.observed_at)], [sec])

    def test_result_order_is_stable_for_all_input_permutations(self):
        rows = [observation("SEC"), observation("YAHOO"), self.yfinance(36)]
        expected = select_effective_observations(rows)
        for order in permutations(rows):
            self.assertEqual(select_effective_observations(order), expected)

    def test_ineligible_sec_allows_yahoo_fallback(self):
        sec = observation("SEC", selection_eligibility="INELIGIBLE")
        yahoo = observation("YAHOO")
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo])], [yahoo])

    def test_structural_conflict_does_not_create_a_silent_yahoo_fallback(self):
        sec, yahoo = observation("SEC"), observation("YAHOO", currency="EUR")
        result = select_effective_observations([sec, yahoo])
        self.assertEqual([x.observation for x in result], [sec])
        self.assertEqual(result[0].comparison.comparison_status, "NOT_COMPARED")

    def test_ambiguous_ts_snapshots_do_not_promote_yfinance(self):
        ts = observation("YAHOO")
        conflicting = replace(ts, id=204, raw_id=4, value=Decimal("90"))
        for order in permutations([ts, conflicting, self.yfinance()]):
            self.assertEqual(select_effective_observations(order), [])

    def test_ambiguous_sec_does_not_promote_yahoo(self):
        sec = observation("SEC")
        conflicting = replace(sec, id=104, value=Decimal("90"))
        self.assertEqual(select_effective_observations([sec, conflicting, observation("YAHOO")]), [])

    def test_yahoo_structural_conflict_is_withheld_instead_of_merged(self):
        ts = observation("YAHOO")
        yf = self.yfinance(currency="EUR")
        self.assertEqual([x.observation for x in select_effective_observations([yf, ts])], [ts])

    def test_identical_repeated_input_does_not_create_a_material_tie(self):
        sec = observation("SEC")
        self.assertEqual(select_effective_observations([sec, sec]), select_effective_observations([sec]))

    def test_zero_does_not_hide_sign_conflict_depending_on_input_order(self):
        zero = observation("SEC", "0")
        positive = replace(zero, id=102, raw_id=2, value=Decimal("1"))
        negative = replace(zero, id=103, raw_id=3, value=Decimal("-1"))
        for order in permutations([zero, positive, negative]):
            self.assertEqual(select_effective_observations(order), [])


if __name__ == "__main__":
    unittest.main()
