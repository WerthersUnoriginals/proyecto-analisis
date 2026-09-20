"""Contract tests for the read-only effective-fundamentals C adapter."""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from c_score_v1 import build_c_score
from database.c_fundamentals_adapter import (
    _effective_observations,
    build_c_fundamental_report,
    load_c_fundamental_report,
)
from database.effective_fundamentals import (
    EffectiveObservation,
    annual_comparisons_by_source,
    growth_yoy_by_source,
)


EXPECTED_KEYS = {
    "latest_eps_yoy_pct",
    "previous_eps_yoy_pct",
    "eps_acceleration_pp",
    "latest_revenue_yoy_pct",
    "previous_revenue_yoy_pct",
    "revenue_acceleration_pp",
    "latest_eps",
    "eps_yoy_pct",
    "eps_loss_to_profit",
}


def effective_row(metric, source, day, value, row_id):
    unit = "USD/shares" if metric == "EPS_DILUTED" else "USD"
    variant = "sec.company_facts" if source == "SEC" else "yahoo.fundamentals_timeseries"
    source_metric = {
        ("EPS_DILUTED", "SEC"): "EarningsPerShareDiluted",
        ("EPS_DILUTED", "YAHOO"): "quarterlyDilutedEPS",
        ("REVENUE", "SEC"): "RevenueFromContractWithCustomerExcludingAssessedTax",
        ("REVENUE", "YAHOO"): "quarterlyTotalRevenue",
    }[(metric, source)]
    observed_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return {
        "company_id": 1,
        "metric": metric,
        "fiscal_year": day.year,
        "fiscal_quarter": 1,
        "canonical_period_end": day,
        "series_date": day,
        "value": Decimal(value),
        "unit": unit,
        "currency": "USD",
        "source": source,
        "source_variant": variant,
        "observation_kind": "REPORTED",
        "selected_observation_id": row_id + 1000,
        "raw_id": row_id,
        "source_metric_name": source_metric,
        "source_unit": unit,
        "source_scale_factor": Decimal("1"),
        "source_period_start": None,
        "source_period_end": day,
        "filed_date": day + timedelta(days=30) if source == "SEC" else None,
        "source_available_at": observed_at,
        "observed_at": observed_at,
        "normalizer_version": "sec-normalized-v2" if source == "SEC" else "yahoo-normalized-v2",
        "intrinsic_quality_status": "OK",
        "selection_eligibility": "ELIGIBLE",
        "selection_policy_version": "c-v2.6-compatible-v1",
        "selection_reason": "TEST_EFFECTIVE",
        "comparison_rules_version": "sec-yahoo-comparison-v1",
        "comparison_status": "NOT_COMPARED",
        "comparison_reason": "NO_COMPARABLE",
        "comparison_reference_id": None,
        "comparison_difference_pct": None,
        "alignment_method": "EXACT",
        "alignment_days": 0,
        "alignment_reference_id": None,
    }


def two_metric_fixture():
    return [
        effective_row("EPS_DILUTED", "SEC", date(2024, 3, 31), "1", 1),
        effective_row("EPS_DILUTED", "SEC", date(2024, 6, 30), "2", 2),
        effective_row("EPS_DILUTED", "SEC", date(2025, 3, 31), "1.2", 3),
        effective_row("EPS_DILUTED", "SEC", date(2025, 6, 30), "3", 4),
        effective_row("REVENUE", "SEC", date(2024, 3, 31), "100", 5),
        effective_row("REVENUE", "SEC", date(2024, 6, 30), "200", 6),
        effective_row("REVENUE", "SEC", date(2025, 3, 31), "110", 7),
        effective_row("REVENUE", "SEC", date(2025, 6, 30), "240", 8),
    ]


class AdapterContractTests(unittest.TestCase):
    def test_converts_view_rows_and_builds_exact_contract_independent_of_order(self):
        rows = two_metric_fixture()
        converted = _effective_observations(rows)

        self.assertTrue(all(isinstance(item, EffectiveObservation) for item in converted))
        first = converted[0]
        self.assertEqual(
            (
                first.observation.company_id,
                first.observation.metric,
                first.observation.source,
                first.observation.source_variant,
                first.observation.series_date,
                first.observation.value,
                first.observation.raw_id,
                first.observation.id,
                first.selection_reason,
            ),
            (
                1,
                "EPS_DILUTED",
                "SEC",
                "sec.company_facts",
                date(2024, 3, 31),
                Decimal("1"),
                1,
                1001,
                "TEST_EFFECTIVE",
            ),
        )
        self.assertEqual(first.observation.intrinsic_quality_reasons, ())
        self.assertEqual(first.comparison.comparison_reasons, ("NO_COMPARABLE",))

        expected = build_c_fundamental_report(rows)
        self.assertEqual(build_c_fundamental_report(list(reversed(rows))), expected)
        self.assertEqual(set(expected), EXPECTED_KEYS)
        self.assertNotIn("data_integrity", expected)
        self.assertNotIn("split_integrity_status", expected)

    def test_growth_values_latest_eps_and_chronological_history(self):
        report = build_c_fundamental_report(two_metric_fixture())

        self.assertAlmostEqual(report["latest_eps_yoy_pct"], 50.0)
        self.assertAlmostEqual(report["previous_eps_yoy_pct"], 20.0)
        self.assertAlmostEqual(report["eps_acceleration_pp"], 30.0)
        self.assertAlmostEqual(report["latest_revenue_yoy_pct"], 20.0)
        self.assertAlmostEqual(report["previous_revenue_yoy_pct"], 10.0)
        self.assertAlmostEqual(report["revenue_acceleration_pp"], 10.0)
        self.assertEqual(report["latest_eps"], 3.0)
        self.assertEqual(report["eps_yoy_pct"], [
            {"date": "2025-03-31", "value": 20.0},
            {"date": "2025-06-30", "value": 50.0},
        ])

    def test_empty_and_missing_metric_cases_use_none_and_empty_list(self):
        empty = build_c_fundamental_report([])
        self.assertEqual(set(empty), EXPECTED_KEYS)
        self.assertTrue(all(
            empty[key] is None
            for key in EXPECTED_KEYS - {"eps_yoy_pct", "eps_loss_to_profit"}
        ))
        self.assertEqual(empty["eps_yoy_pct"], [])
        self.assertFalse(empty["eps_loss_to_profit"])

        only_eps = build_c_fundamental_report(two_metric_fixture()[:4])
        self.assertIsNone(only_eps["latest_revenue_yoy_pct"])
        only_revenue = build_c_fundamental_report(two_metric_fixture()[4:])
        self.assertIsNone(only_revenue["latest_eps"])
        self.assertEqual(only_revenue["eps_yoy_pct"], [])

    def test_one_growth_has_no_previous_or_acceleration_and_history_is_not_padded(self):
        rows = [
            effective_row("EPS_DILUTED", "SEC", date(2024, 6, 30), "2", 1),
            effective_row("EPS_DILUTED", "SEC", date(2025, 6, 30), "3", 2),
        ]

        report = build_c_fundamental_report(rows)
        self.assertEqual(report["latest_eps_yoy_pct"], 50.0)
        self.assertIsNone(report["previous_eps_yoy_pct"])
        self.assertIsNone(report["eps_acceleration_pp"])
        self.assertEqual(report["eps_yoy_pct"], [{"date": "2025-06-30", "value": 50.0}])

    def test_observation_without_annual_comparable_has_no_growth(self):
        row = effective_row("EPS_DILUTED", "SEC", date(2025, 6, 30), "3", 1)

        report = build_c_fundamental_report([row])
        self.assertEqual(report["latest_eps"], 3.0)
        self.assertIsNone(report["latest_eps_yoy_pct"])
        self.assertEqual(report["eps_yoy_pct"], [])
        self.assertFalse(report["eps_loss_to_profit"])

    def test_loss_to_profit_uses_annual_pair_even_when_yoy_is_not_defined(self):
        cases = (("-1", "1", True), ("0", "1", True), ("1", "2", False), ("-1", "-0.5", False))
        for previous, current, expected in cases:
            with self.subTest(previous=previous, current=current):
                rows = [
                    effective_row("EPS_DILUTED", "SEC", date(2024, 6, 30), previous, 1),
                    effective_row("EPS_DILUTED", "SEC", date(2025, 6, 30), current, 2),
                ]
                converted = _effective_observations(rows)
                comparisons = annual_comparisons_by_source(converted, "EPS_DILUTED")
                growth = growth_yoy_by_source(converted)
                report = build_c_fundamental_report(rows)

                self.assertEqual(len(comparisons), 1)
                self.assertEqual(len(growth), 1 if Decimal(previous) > 0 else 0)
                self.assertEqual(report["eps_loss_to_profit"], expected)

    def test_latest_yahoo_without_yahoo_comparable_never_uses_sec(self):
        rows = [
            effective_row("EPS_DILUTED", "SEC", date(2023, 6, 30), "-1", 1),
            effective_row("EPS_DILUTED", "SEC", date(2024, 6, 30), "1", 2),
            effective_row("EPS_DILUTED", "YAHOO", date(2025, 9, 30), "2", 3),
        ]

        report = build_c_fundamental_report(rows)
        self.assertEqual(report["latest_eps"], 2.0)
        self.assertIsNone(report["latest_eps_yoy_pct"])
        self.assertEqual(report["eps_yoy_pct"], [])
        self.assertFalse(report["eps_loss_to_profit"])

    @patch("database.c_fundamentals_adapter.load_effective_current")
    def test_loader_delegates_to_current_view_then_builds(self, load_effective):
        load_effective.return_value = two_metric_fixture()

        result = load_c_fundamental_report(7)

        load_effective.assert_called_once_with(7)
        self.assertEqual(result, build_c_fundamental_report(two_metric_fixture()))


def aapl_fixture():
    raw_rows = [
        ("EPS_DILUTED", "SEC", "2024-03-30", "1.53", 25),
        ("EPS_DILUTED", "SEC", "2024-06-29", "1.40", 27),
        ("EPS_DILUTED", "SEC", "2024-12-28", "2.40", 29),
        ("EPS_DILUTED", "SEC", "2025-03-29", "1.65", 31),
        ("EPS_DILUTED", "SEC", "2025-06-28", "1.57", 33),
        ("EPS_DILUTED", "YAHOO", "2025-09-30", "1.85", 409),
        ("EPS_DILUTED", "SEC", "2025-12-27", "2.84", 34),
        ("EPS_DILUTED", "SEC", "2026-03-28", "2.01", 35),
        ("EPS_DILUTED", "SEC", "2026-06-27", "2.02", 36),
        ("REVENUE", "SEC", "2025-03-29", "95359000000", 65),
        ("REVENUE", "SEC", "2025-06-28", "94036000000", 67),
        ("REVENUE", "YAHOO", "2025-09-30", "102466000000", 414),
        ("REVENUE", "SEC", "2026-03-28", "111184000000", 69),
        ("REVENUE", "SEC", "2026-06-27", "109417000000", 70),
    ]
    return [effective_row(metric, source, date.fromisoformat(day), value, row_id)
            for metric, source, day, value, row_id in raw_rows]


class AaplCompatibilityTests(unittest.TestCase):
    def test_aapl_effective_fixture_reproduces_growth_and_legacy_trend(self):
        report = build_c_fundamental_report(aapl_fixture())

        expected = (
            ("latest_eps_yoy_pct", 28.662420382),
            ("previous_eps_yoy_pct", 21.818181818),
            ("eps_acceleration_pp", 6.844238564),
            ("latest_revenue_yoy_pct", 16.356501765),
            ("previous_revenue_yoy_pct", 16.595182416),
            ("revenue_acceleration_pp", -0.238680651),
        )
        for key, value in expected:
            self.assertAlmostEqual(report[key], value, places=9)
        self.assertEqual(report["latest_eps"], 2.02)
        self.assertFalse(report["eps_loss_to_profit"])
        self.assertEqual(
            [item["date"] for item in report["eps_yoy_pct"][-4:]],
            ["2025-06-28", "2025-12-27", "2026-03-28", "2026-06-27"],
        )
        expected_growth = (12.142857143, 18.333333333, 21.818181818, 28.662420382)
        for item, value in zip(report["eps_yoy_pct"][-4:], expected_growth):
            self.assertAlmostEqual(item["value"], value, places=9)

        components = build_c_score(report)["c_score_v1"]["components"]
        self.assertEqual(components["eps_trend_quality"]["points"], 7.0)
        self.assertEqual(components["persistence"]["points"], 5.0)


if __name__ == "__main__":
    unittest.main()
