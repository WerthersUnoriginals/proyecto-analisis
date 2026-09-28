import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.split_integrity import (
    assess_split_integrity,
    current_yoy_crosses_split,
    reconstruct_split_integrity,
    resolve_legacy_sec_metric,
)


UTC = timezone.utc


def sec(metric, tag, end, value, filed, observed, raw_id, eligible="ELIGIBLE"):
    return {
        "raw_id": raw_id,
        "metric": metric,
        "source": "SEC",
        "source_variant": "sec.company_facts",
        "normalizer_version": "sec-normalized-v2",
        "source_metric_name": tag,
        "source_period_end": end,
        "filed_date": filed,
        "observed_at": observed,
        "selection_eligibility": eligible,
        "value": Decimal(value),
    }


class SecResolverGoldenMasterTests(unittest.TestCase):
    def test_tag_coverage_selects_the_series_with_more_periods(self):
        obs = [
            sec("REVENUE", "RevenueFromContractWithCustomerExcludingAssessedTax", date(2024, 3, 30), "1", date(2024, 5, 1), datetime(2024, 5, 2, tzinfo=UTC), 1),
            sec("REVENUE", "RevenueFromContractWithCustomerExcludingAssessedTax", date(2024, 6, 30), "2", date(2024, 8, 1), datetime(2024, 8, 2, tzinfo=UTC), 2),
            sec("REVENUE", "Revenues", date(2024, 3, 30), "1", date(2024, 5, 1), datetime(2024, 5, 2, tzinfo=UTC), 3),
            sec("REVENUE", "Revenues", date(2024, 6, 30), "2", date(2024, 8, 1), datetime(2024, 8, 2, tzinfo=UTC), 4),
            sec("REVENUE", "Revenues", date(2024, 9, 30), "3", date(2024, 11, 1), datetime(2024, 11, 2, tzinfo=UTC), 5),
        ]
        result = resolve_legacy_sec_metric(obs, "REVENUE", datetime(2025, 1, 1, tzinfo=UTC), date(2020, 1, 1))
        self.assertEqual(result.selected_tag, "Revenues")
        self.assertEqual(len(result.values), 3)

    def test_candidate_order_breaks_equal_coverage_tie(self):
        common = dict(end=date(2024, 3, 30), value="1", filed=date(2024, 5, 1), observed=datetime(2024, 5, 2, tzinfo=UTC))
        obs = [
            sec("REVENUE", "Revenues", raw_id=2, **common),
            sec("REVENUE", "RevenueFromContractWithCustomerExcludingAssessedTax", raw_id=1, **common),
        ]
        result = resolve_legacy_sec_metric(obs, "REVENUE", datetime(2025, 1, 1, tzinfo=UTC), date(2020, 1, 1))
        self.assertEqual(result.selected_tag, "RevenueFromContractWithCustomerExcludingAssessedTax")

    def test_latest_filing_wins_for_a_period(self):
        end = date(2024, 3, 30)
        obs = [
            sec("EPS_DILUTED", "EarningsPerShareDiluted", end, "1.00", date(2024, 5, 1), datetime(2024, 5, 2, tzinfo=UTC), 1),
            sec("EPS_DILUTED", "EarningsPerShareDiluted", end, "1.10", date(2024, 6, 1), datetime(2024, 6, 2, tzinfo=UTC), 2),
        ]
        result = resolve_legacy_sec_metric(obs, "EPS_DILUTED", datetime(2025, 1, 1, tzinfo=UTC), date(2020, 1, 1))
        self.assertEqual(result.values[end], Decimal("1.10"))

    def test_equal_filed_date_conflicting_values_fail_closed(self):
        end = date(2024, 3, 30)
        obs = [
            sec("EPS_DILUTED", "EarningsPerShareDiluted", end, "1.00", date(2024, 5, 1), datetime(2024, 5, 2, tzinfo=UTC), 1),
            sec("EPS_DILUTED", "EarningsPerShareDiluted", end, "1.20", date(2024, 5, 1), datetime(2024, 5, 2, tzinfo=UTC), 2),
        ]
        result = resolve_legacy_sec_metric(obs, "EPS_DILUTED", datetime(2025, 1, 1, tzinfo=UTC), date(2020, 1, 1))
        self.assertEqual(result.values, {})
        self.assertEqual(result.ambiguous_periods, (end,))

    def test_diluted_shares_tag_quality_matches_legacy(self):
        observed = datetime(2024, 5, 2, tzinfo=UTC)
        for tag, quality in (
            ("WeightedAverageNumberOfDilutedSharesOutstanding", "DILUTED_EXACT"),
            ("WeightedAverageNumberOfShareOutstandingBasicAndDiluted", "BASIC_AND_DILUTED"),
            ("WeightedAverageNumberOfSharesOutstandingBasic", "BASIC_FALLBACK"),
        ):
            with self.subTest(tag=tag):
                result = resolve_legacy_sec_metric(
                    [sec("DILUTED_SHARES", tag, date(2024, 3, 30), "100", date(2024, 5, 1), observed, 1)],
                    "DILUTED_SHARES", datetime(2025, 1, 1, tzinfo=UTC), date(2020, 1, 1),
                    candidate_tags=(tag,),
                )
                self.assertEqual(result.shares_quality, quality)

    def test_amendment_visibility_respects_as_of(self):
        end = date(2024, 3, 30)
        original = sec("EPS_DILUTED", "EarningsPerShareDiluted", end, "1.00", date(2024, 5, 1), datetime(2024, 5, 2, tzinfo=UTC), 1)
        amended = sec("EPS_DILUTED", "EarningsPerShareDiluted", end, "1.20", date(2024, 7, 1), datetime(2024, 7, 2, tzinfo=UTC), 2)
        before = resolve_legacy_sec_metric((original, amended), "EPS_DILUTED", datetime(2024, 6, 1, tzinfo=UTC), date(2020, 1, 1))
        after = resolve_legacy_sec_metric((original, amended), "EPS_DILUTED", datetime(2024, 8, 1, tzinfo=UTC), date(2020, 1, 1))
        self.assertEqual(before.values[end], Decimal("1.00"))
        self.assertEqual(after.values[end], Decimal("1.20"))

    def test_unknown_tag_order_is_not_silently_certified(self):
        tag = "EntitySpecificDilutedWeightedAverageShares"
        result = resolve_legacy_sec_metric(
            [sec("DILUTED_SHARES", tag, date(2024, 3, 30), "100", date(2024, 5, 1), datetime(2024, 5, 2, tzinfo=UTC), 1)],
            "DILUTED_SHARES", datetime(2025, 1, 1, tzinfo=UTC), date(2020, 1, 1),
        )
        self.assertFalse(result.candidate_order_demonstrated)
        assessment = assess_split_integrity(
            "SUCCESS", "COMPLETE", ((date(2020, 8, 31), Decimal("4")),),
            {date(2020, 6, 27): Decimal("2"), date(2020, 9, 26): Decimal("2")},
            {date(2020, 6, 27): Decimal("200"), date(2020, 9, 26): Decimal("200")},
            {date(2020, 6, 27): Decimal("100"), date(2020, 9, 26): Decimal("100")},
            result.shares_quality,
            candidate_order_demonstrated=result.candidate_order_demonstrated,
        )
        self.assertEqual(assessment.status, "REVIEW_REQUIRED")


class SplitIntegrityGoldenMasterTests(unittest.TestCase):
    def test_yoy_crossing_uses_legacy_open_closed_interval(self):
        self.assertFalse(current_yoy_crosses_split((date(2024, 3, 31),), date(2024, 3, 31), date(2024, 6, 30), "SUCCESS", "COMPLETE"))
        self.assertTrue(current_yoy_crosses_split((date(2024, 4, 15),), date(2024, 3, 31), date(2024, 6, 30), "SUCCESS", "COMPLETE"))
        self.assertTrue(current_yoy_crosses_split((date(2024, 6, 30),), date(2024, 3, 31), date(2024, 6, 30), "SUCCESS", "COMPLETE"))
        self.assertFalse(current_yoy_crosses_split((date(2024, 6, 30),), date(2024, 6, 30), date(2024, 9, 30), "SUCCESS", "COMPLETE"))

    def test_yoy_crossing_handles_multiple_splits_and_unknown_capture(self):
        splits = (date(2024, 2, 1), date(2024, 8, 1))
        self.assertTrue(current_yoy_crosses_split(splits, date(2024, 1, 1), date(2024, 9, 1), "SUCCESS", "COMPLETE"))
        self.assertIsNone(current_yoy_crosses_split(splits, date(2024, 1, 1), date(2024, 9, 1), "FAILED", "UNKNOWN"))
        self.assertIsNone(current_yoy_crosses_split(splits, date(2024, 1, 1), date(2024, 9, 1), "PARTIAL", "INCOMPLETE"))
        self.assertIsNone(current_yoy_crosses_split(splits, None, date(2024, 9, 1), "SUCCESS", "COMPLETE"))

    def test_capture_statuses_map_without_inventing_evidence(self):
        self.assertEqual(assess_split_integrity("SUCCESS", "COMPLETE", (), {}, {}, {}, "DILUTED_EXACT", candidate_order_demonstrated=True).status, "NO_RECENT_SPLITS")
        self.assertEqual(assess_split_integrity("FAILED", "UNKNOWN", (), {}, {}, {}, "DILUTED_EXACT", candidate_order_demonstrated=True).status, "UNKNOWN")
        self.assertEqual(assess_split_integrity("PARTIAL", "INCOMPLETE", (), {}, {}, {}, "DILUTED_EXACT", candidate_order_demonstrated=True).status, "UNKNOWN")

    def test_adjusted_unadjusted_and_ambiguous_scenarios(self):
        events = ((date(2020, 8, 31), Decimal("4")),)
        eps = {date(2020, 6, 27): Decimal("2"), date(2020, 9, 26): Decimal("2")}
        adjusted_net_income = {date(2020, 6, 27): Decimal("200"), date(2020, 9, 26): Decimal("200")}
        unadjusted_net_income = {date(2020, 6, 27): Decimal("200"), date(2020, 9, 26): Decimal("800")}
        adjusted_shares = {date(2020, 6, 27): Decimal("100"), date(2020, 9, 26): Decimal("100")}
        unadjusted_shares = {date(2020, 6, 27): Decimal("100"), date(2020, 9, 26): Decimal("400")}
        self.assertEqual(assess_split_integrity("SUCCESS", "COMPLETE", events, eps, adjusted_net_income, adjusted_shares, "DILUTED_EXACT", candidate_order_demonstrated=True).status, "VERIFIED_ALREADY_ADJUSTED")
        self.assertEqual(assess_split_integrity("SUCCESS", "COMPLETE", events, eps, unadjusted_net_income, unadjusted_shares, "DILUTED_EXACT", candidate_order_demonstrated=True).status, "UNADJUSTED_DETECTED")
        self.assertEqual(assess_split_integrity("SUCCESS", "COMPLETE", events, eps, adjusted_net_income, adjusted_shares, "BASIC_FALLBACK", candidate_order_demonstrated=True).status, "REVIEW_REQUIRED")

    def test_multiple_splits_aggregate_unadjusted_over_adjusted(self):
        events = (
            (date(2020, 8, 31), Decimal("4")),
            (date(2024, 6, 10), Decimal("2")),
        )
        eps = {
            date(2020, 6, 27): Decimal("2"), date(2020, 9, 26): Decimal("2"),
            date(2024, 3, 30): Decimal("2"), date(2024, 6, 29): Decimal("2"),
        }
        net_income = {
            date(2020, 6, 27): Decimal("200"), date(2020, 9, 26): Decimal("200"),
            date(2024, 3, 30): Decimal("200"), date(2024, 6, 29): Decimal("400"),
        }
        shares = {
            date(2020, 6, 27): Decimal("100"), date(2020, 9, 26): Decimal("100"),
            date(2024, 3, 30): Decimal("100"), date(2024, 6, 29): Decimal("200"),
        }
        result = assess_split_integrity("SUCCESS", "COMPLETE", events, eps, net_income, shares, "DILUTED_EXACT", candidate_order_demonstrated=True)
        self.assertEqual(result.status, "UNADJUSTED_DETECTED")
        self.assertEqual([item["status"] for item in result.events], ["ALREADY_ADJUSTED", "UNADJUSTED"])

    def test_reconstruct_selects_latest_visible_capture_and_does_not_fallback(self):
        class Capture:
            def __init__(self, ident, observed, acquisition, completeness):
                self.id = ident
                self.observed_at = observed
                self.acquisition_status = acquisition
                self.completeness_status = completeness

        success = Capture(1, datetime(2025, 1, 1, tzinfo=UTC), "SUCCESS", "COMPLETE")
        failed = Capture(2, datetime(2025, 2, 1, tzinfo=UTC), "FAILED", "UNKNOWN")
        result = reconstruct_split_integrity(
            (success, failed), {1: (), 2: ()}, datetime(2025, 3, 1, tzinfo=UTC),
            {}, {}, {}, "DILUTED_EXACT", candidate_order_demonstrated=True,
        )
        self.assertEqual(result.status, "UNKNOWN")

    def test_reconstruct_successful_zero_event_capture_is_no_recent(self):
        class Capture:
            id = 1
            observed_at = datetime(2025, 1, 1, tzinfo=UTC)
            acquisition_status = "SUCCESS"
            completeness_status = "COMPLETE"

        result = reconstruct_split_integrity(
            (Capture(),), {1: ()}, datetime(2025, 2, 1, tzinfo=UTC),
            {}, {}, {}, "DILUTED_EXACT", candidate_order_demonstrated=True,
        )
        self.assertEqual(result.status, "NO_RECENT_SPLITS")


if __name__ == "__main__":
    unittest.main()
