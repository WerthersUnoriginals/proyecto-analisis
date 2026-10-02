"""Tests for C Score v1.3: losses are unfavorable evidence, not missing data."""

import unittest
from datetime import date, datetime, timezone

from c_score_v1 import build_c_score as build_v12
from c_score_v13 import MODEL_VERSION, build_c_score
from database.c_contract_v3 import build_c_contract_v3
from database.quarterly_v3 import build_quarterly_view
from database.split_basis import SplitReconciliation
from test_quarterly_v3 import calendar_year_company, real_facts

UTC = timezone.utc
NO_SPLITS = SplitReconciliation("NO_RECENT_SPLITS", ())


def report(statuses, *, latest_eps=1.0, comparable_eps=0.5, sales=(30.0, 25.0), integrity="VERIFIED"):
    """Build a contract-shaped report from (quarter, status, yoy) tuples, oldest first."""
    quarters = [{"quarter": q, "status": s, "yoy_pct": y} for q, s, y in statuses]
    latest, previous = quarters[-1], quarters[-2]
    history = [{"date": q["quarter"], "value": q["yoy_pct"]} for q in quarters if q["status"] == "GROWTH"]
    latest_yoy = latest["yoy_pct"] if latest["status"] == "GROWTH" else None
    previous_yoy = previous["yoy_pct"] if previous["status"] == "GROWTH" else None
    return {
        "latest_eps_yoy_pct": latest_yoy,
        "previous_eps_yoy_pct": previous_yoy,
        "eps_acceleration_pp": None if latest_yoy is None or previous_yoy is None else latest_yoy - previous_yoy,
        "latest_revenue_yoy_pct": sales[0],
        "previous_revenue_yoy_pct": sales[1],
        "revenue_acceleration_pp": sales[0] - sales[1],
        "latest_eps": latest_eps,
        "eps_yoy_pct": history,
        "eps_loss_to_profit": latest["status"] == "LOSS_TO_PROFIT",
        "data_integrity": integrity,
        "split_integrity_status": "NO_RECENT_SPLITS",
        "eps_growth_detail": {
            "latest": {**latest, "comparable_eps": comparable_eps},
            "previous": previous,
            "quarters": quarters,
        },
    }


GROWTH_ONLY = [("Q1", "GROWTH", 20.0), ("Q2", "GROWTH", 26.0), ("Q3", "GROWTH", 30.0),
               ("Q4", "GROWTH", 35.0), ("Q5", "GROWTH", 40.0)]


class EquivalenceTests(unittest.TestCase):
    def test_all_growth_quarters_score_exactly_like_v12(self):
        data = report(GROWTH_ONLY)
        legacy = {key: value for key, value in data.items() if key != "eps_growth_detail"}
        self.assertEqual(build_c_score(data)["c_score_v1"]["normalized_score"],
                         build_v12(legacy)["c_score_v1"]["normalized_score"])
        self.assertEqual(build_c_score(data)["c_score_v1"]["model_version"], MODEL_VERSION)
        self.assertEqual(MODEL_VERSION, "1.3-exp")

    def test_real_companies_score_like_v12(self):
        cases = {"aapl": datetime(2026, 9, 3, tzinfo=UTC), "nvda": datetime(2026, 10, 2, tzinfo=UTC)}
        for ticker, as_of in cases.items():
            with self.subTest(ticker=ticker):
                view = build_quarterly_view(real_facts(ticker), [], split_events=(), as_of=as_of)
                contract = build_c_contract_v3(view, NO_SPLITS, company_id=1, as_of=as_of)
                self.assertEqual(build_c_score(contract)["c_score_v1"]["normalized_score"],
                                 build_v12(contract)["c_score_v1"]["normalized_score"])


class LossTests(unittest.TestCase):
    def test_persistent_losses_are_unfavorable_not_missing(self):
        statuses = [("Q1", "LOSS", None), ("Q2", "LOSS", None), ("Q3", "LOSS", None),
                    ("Q4", "LOSS", None), ("Q5", "LOSS", None)]
        data = report(statuses, latest_eps=-0.4, comparable_eps=-0.2, sales=(60.0, 50.0))
        legacy_score = build_v12({k: v for k, v in data.items() if k != "eps_growth_detail"})["c_score_v1"]
        score = build_c_score(data)["c_score_v1"]
        components = score["components"]
        self.assertTrue(components["eps_growth"]["available"])
        self.assertEqual(components["eps_growth"]["points"], 0.0)
        self.assertEqual(components["eps_acceleration"]["points"], 0.0)
        self.assertEqual(components["persistence"]["points"], 0.0)
        self.assertEqual(components["eps_trend_quality"]["points"], 0.0)
        self.assertGreater(legacy_score["normalized_score"], 85)  # the v1.2 defect
        self.assertLess(score["normalized_score"], 35)
        self.assertEqual(score["status"], "OK")

    def test_fall_into_loss_scores_zero_growth(self):
        statuses = GROWTH_ONLY[:4] + [("Q5", "LOSS", None)]
        score = build_c_score(report(statuses, latest_eps=-0.1, comparable_eps=-0.05))["c_score_v1"]
        self.assertEqual(score["components"]["eps_growth"]["points"], 0.0)
        self.assertTrue(score["components"]["eps_growth"]["available"])

    def test_loss_to_profit_is_partial_and_reviewed(self):
        statuses = [("Q1", "LOSS", None), ("Q2", "LOSS", None), ("Q3", "LOSS", None),
                    ("Q4", "LOSS", None), ("Q5", "LOSS_TO_PROFIT", None)]
        result = build_c_score(report(statuses, latest_eps=0.3, comparable_eps=-0.2))
        score = result["c_score_v1"]
        self.assertFalse(score["components"]["eps_growth"]["available"])
        self.assertEqual(score["components"]["eps_growth"]["status"], "LOSS_TO_PROFIT")
        self.assertEqual(score["usability"], "C_SCORE_REVIEW")
        self.assertEqual(result["c_classic"]["result"], "PASS")

    def test_growth_after_loss_quarter_flags_base_effect(self):
        statuses = GROWTH_ONLY[:3] + [("Q4", "LOSS", None), ("Q5", "GROWTH", 80.0)]
        result = build_c_score(report(statuses))
        self.assertFalse(result["c_score_v1"]["components"]["eps_acceleration"]["available"])
        self.assertIn("BASE_EFFECT_RISK", result["c_flags"])

    def test_trend_window_skips_one_missing_quarter_like_v12(self):
        statuses = [("Q1", "GROWTH", 20.0), ("Q2", "GROWTH", 26.0), ("Q3", "NO_DATA", None),
                    ("Q4", "GROWTH", 35.0), ("Q5", "GROWTH", 40.0)]
        data = report(statuses)
        components = build_c_score(data)["c_score_v1"]["components"]
        legacy = build_v12({k: v for k, v in data.items() if k != "eps_growth_detail"})["c_score_v1"]["components"]
        self.assertEqual(components["persistence"]["points"], legacy["persistence"]["points"])
        self.assertEqual(components["eps_trend_quality"]["points"], legacy["eps_trend_quality"]["points"])

    def test_loss_inside_trend_window_counts_against(self):
        statuses = GROWTH_ONLY[:2] + [("Q3", "LOSS", None), ("Q4", "GROWTH", 35.0), ("Q5", "GROWTH", 40.0)]
        components = build_c_score(report(statuses))["c_score_v1"]["components"]
        # Strong quarters: 26, 35, 40 -> 3 (6 pts); stability: not all positive, not min>=10;
        # range of growth values 40-26 <= 50 -> +1.
        self.assertEqual(components["persistence"]["points"], 7.0)

    def test_small_base_uses_real_comparable(self):
        statuses = GROWTH_ONLY[:4] + [("Q5", "GROWTH", 400.0)]
        result = build_c_score(report(statuses, latest_eps=0.25, comparable_eps=0.05))
        self.assertEqual(result["c_score_v1"]["small_base"]["comparable_eps"], 0.05)
        self.assertIn("SMALL_BASE_RISK", result["c_flags"])

    def test_legacy_report_without_detail_detects_current_loss(self):
        data = report(GROWTH_ONLY)
        data.pop("eps_growth_detail")
        data.update({"latest_eps": -0.3, "latest_eps_yoy_pct": None, "eps_acceleration_pp": None})
        components = build_c_score(data)["c_score_v1"]["components"]
        self.assertTrue(components["eps_growth"]["available"])
        self.assertEqual(components["eps_growth"]["points"], 0.0)


class ContractDetailTests(unittest.TestCase):
    def test_contract_exposes_quarter_statuses(self):
        keys = [(2024, 1), (2024, 2), (2024, 3), (2025, 1), (2025, 2), (2025, 3)]
        facts = calendar_year_company(dict(zip(keys, ["-1", "-1", "-1", "-2", "0.5", "1.2"])))
        as_of = datetime(2025, 11, 15, tzinfo=UTC)
        view = build_quarterly_view(facts, [], split_events=(), as_of=as_of)
        detail = build_c_contract_v3(view, NO_SPLITS, company_id=1, as_of=as_of)["eps_growth_detail"]
        self.assertEqual(detail["latest"]["status"], "LOSS_TO_PROFIT")
        self.assertEqual(detail["latest"]["comparable_eps"], -1.0)
        self.assertEqual(detail["previous"]["status"], "LOSS_TO_PROFIT")
        statuses = [item["status"] for item in detail["quarters"]]
        self.assertEqual(statuses, ["LOSS", "NO_DATA", "LOSS", "LOSS_TO_PROFIT", "LOSS_TO_PROFIT"])


if __name__ == "__main__":
    unittest.main()
