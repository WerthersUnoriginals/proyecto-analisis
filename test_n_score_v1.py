"""Tests for N Score v1 (weights approved by the human on 2026-10-03)."""

import unittest
from datetime import date

from n_score_v1 import MODEL_VERSION, build_n_score
from test_catalysts_v3 import filing
from test_n_contract_v1 import AS_OF, contract, long_series
from test_new_highs_v1 import series


def report(**overrides):
    base = {
        "pct_below_high_52w": 0.0, "new_high_recent": True, "sessions_since_high_52w": 0,
        "pct_below_high_5y": 0.0, "n_status": "OK", "price_data_integrity": "VERIFIED",
        "catalysts": {"counts": {}},
    }
    base.update(overrides)
    return base


def points(data, component):
    return build_n_score(data)["n_score_v1"]["components"][component]["points"]


class ComponentTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(MODEL_VERSION, "n-1.0-exp")

    def test_at_the_high_scores_full_and_passes(self):
        result = build_n_score(report())
        self.assertEqual(result["n_score_v1"]["normalized_score"], 100.0)
        self.assertEqual(result["n_classic"]["result"], "PASS")
        self.assertEqual(result["n_score_v1"]["usability"], "N_SCORE_USABLE")

    def test_distance_to_52_week_high_curve(self):
        for pct, expected in ((0, 60), (5, 55), (7.5, 50), (10, 45), (15, 30), (20, 20), (25, 10), (35, 0), (60, 0)):
            with self.subTest(pct=pct):
                self.assertAlmostEqual(points(report(pct_below_high_52w=pct), "distance_52w"), expected)

    def test_recent_high_steps(self):
        for since, expected in ((0, 25), (19, 25), (20, 12), (59, 12), (60, 0), (200, 0)):
            with self.subTest(since=since):
                self.assertEqual(points(report(sessions_since_high_52w=since, new_high_recent=since < 20),
                                        "recent_high"), expected)

    def test_distance_to_five_year_high_curve(self):
        for pct, expected in ((0, 15), (10, 10), (17.5, 7), (25, 4), (40, 0), (80, 0)):
            with self.subTest(pct=pct):
                self.assertAlmostEqual(points(report(pct_below_high_5y=pct), "distance_5y"), expected)

    def test_missing_five_year_high_renormalizes(self):
        result = build_n_score(report(pct_below_high_5y=None, price_data_integrity="VERIFIED_WITH_PARTIAL_CORE_DATA"))
        score = result["n_score_v1"]
        self.assertEqual((score["available_points"], score["normalized_score"]), (85.0, 100.0))
        self.assertEqual(score["status"], "OK")
        self.assertEqual(score["usability"], "N_SCORE_REVIEW")


class ClassicTests(unittest.TestCase):
    def test_classic_threshold_is_15_percent(self):
        self.assertEqual(build_n_score(report(pct_below_high_52w=15.0))["n_classic"]["result"], "PASS")
        self.assertEqual(build_n_score(report(pct_below_high_52w=15.01))["n_classic"]["result"], "FAIL_FAR_FROM_HIGH")

    def test_status_results_come_first(self):
        for status in ("INSUFFICIENT_HISTORY", "STALE_PRICES", "NO_PRICE_EVIDENCE"):
            with self.subTest(status=status):
                self.assertEqual(build_n_score(report(n_status=status))["n_classic"]["result"], status)


class StatusTests(unittest.TestCase):
    def test_review_integrity(self):
        score = build_n_score(report(price_data_integrity="REVIEW_REQUIRED"))["n_score_v1"]
        self.assertEqual((score["status"], score["usability"]), ("REVIEW_REQUIRED_DATA", "N_SCORE_REVIEW"))

    def test_recent_ipo_has_no_score(self):
        result = build_n_score(contract(series([100] * 100)))
        score = result["n_score_v1"]
        self.assertIsNone(score["normalized_score"])
        self.assertEqual((score["status"], score["class"]), ("PARTIAL_SCORE", "N/D"))
        self.assertEqual(result["n_classic"]["result"], "INSUFFICIENT_HISTORY")


class ContractIntegrationTests(unittest.TestCase):
    def test_real_contract_shape(self):
        result = build_n_score(contract(long_series(95)))
        self.assertEqual(result["n_classic"]["result"], "PASS")
        # Close 5% below a flat 52-week high: 55 of the 60 distance points.
        components = result["n_score_v1"]["components"]
        self.assertAlmostEqual(components["distance_52w"]["points"], 55.0)

    def test_catalysts_are_flags_only(self):
        from database.catalysts_v3 import select_catalysts

        catalysts = select_catalysts([filing("0001045810-26-000001", date(2026, 9, 1), ["5.02"])], AS_OF)
        with_events = build_n_score(contract(long_series(), catalysts=catalysts))
        without = build_n_score(contract(long_series()))
        self.assertIn("CATALYST_MANAGEMENT_CHANGE", with_events["n_flags"])
        self.assertEqual(with_events["n_score_v1"], without["n_score_v1"])
        self.assertEqual(with_events["n_classic"], without["n_classic"])


if __name__ == "__main__":
    unittest.main()
