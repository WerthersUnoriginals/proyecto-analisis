"""Tests for A Score v1 (weights approved by the human on 2026-10-02)."""

import unittest
from datetime import datetime, timezone

from a_score_v1 import MODEL_VERSION, build_a_score
from database.a_contract_v1 import build_a_contract_v1
from database.annual_v3 import build_annual_view
from database.split_basis import SplitReconciliation
from test_a_contract_v1 import healthy
from test_annual_v3 import NVDA_EVENTS
from test_quarterly_v3 import real_facts

UTC = timezone.utc


def contract(facts, *, as_of=datetime(2026, 3, 15, tzinfo=UTC), splits=SplitReconciliation("NO_RECENT_SPLITS", ())):
    view = build_annual_view(facts, split_events=splits.events, as_of=as_of)
    return build_a_contract_v1(view, splits, company_id=1, as_of=as_of)


def score(data):
    return build_a_score(data)["a_score_v1"]


class ComponentTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(MODEL_VERSION, "a-1.0-exp")

    def test_strong_company_scores_full_and_passes(self):
        eps = {2021: "0.7", 2022: "1.0", 2023: "1.5", 2024: "2.3", 2025: "3.5"}  # CAGR 51.8%
        net_income = {2021: "10", 2022: "14", 2023: "20", 2024: "28", 2025: "40"}
        revenue = {2021: "100", 2022: "125", 2023: "155", 2024: "190", 2025: "240"}
        equity = {2020: "40", 2021: "50", 2022: "60", 2023: "75", 2024: "95", 2025: "120"}
        result = build_a_score(contract(healthy(eps=eps, net_income=net_income, revenue=revenue, equity=equity)))
        self.assertEqual(result["a_score_v1"]["normalized_score"], 100.0)
        self.assertEqual(result["a_classic"]["result"], "PASS")

    def test_curves(self):
        data = contract(healthy())
        components = score(data)["components"]
        # healthy(): EPS 1.3 -> 2.9 over 3 years = 30.66% CAGR -> 25 + 5.66 * 0.3
        self.assertAlmostEqual(components["eps_cagr_3y"]["points"], 25 + (data["eps_cagr_3y_pct"] - 25) * 0.3, places=9)
        self.assertEqual(components["eps_stability"]["points"], 10.0)
        self.assertEqual(components["roe"]["points"], 25.0)  # 26.4%

    def test_loss_year_scores_zero_not_missing(self):
        eps = {2021: "1", 2022: "1.3", 2023: "1.1", 2024: "1.2", 2025: "-0.5"}
        result = build_a_score(contract(healthy(eps=eps)))
        components = result["a_score_v1"]["components"]
        self.assertTrue(components["eps_cagr_3y"]["available"])
        self.assertEqual(components["eps_cagr_3y"]["points"], 0.0)
        self.assertEqual(components["eps_stability"]["points"], 0.0)
        self.assertEqual(result["a_classic"]["result"], "FAIL_LOSS_YEAR")

    def test_loss_to_profit_endpoint_is_unavailable(self):
        eps = {2021: "1", 2022: "-0.5", 2023: "0.2", 2024: "0.6", 2025: "1.2"}
        components = score(contract(healthy(eps=eps)))["components"]
        self.assertFalse(components["eps_cagr_3y"]["available"])
        self.assertEqual(components["eps_cagr_3y"]["status"], "LOSS_TO_PROFIT")
        self.assertFalse(components["eps_consistency"]["available"])

    def test_down_year_costs_stability(self):
        eps = {2021: "1", 2022: "1.3", 2023: "1.2", 2024: "1.6", 2025: "2.1"}
        components = score(contract(healthy(eps=eps)))["components"]
        self.assertEqual(components["eps_stability"]["points"], 4.0)

    def test_non_meaningful_roe_goes_to_review(self):
        equity = {2020: "50", 2021: "60", 2022: "70", 2023: "85", 2024: "-10", 2025: "120"}
        result = build_a_score(contract(healthy(equity=equity)))
        a_score = result["a_score_v1"]
        self.assertFalse(a_score["components"]["roe"]["available"])
        self.assertEqual(a_score["usability"], "A_SCORE_REVIEW")
        self.assertIn("ROE_NOT_MEANINGFUL", result["a_flags"])
        self.assertEqual(result["a_classic"]["result"], "FAIL_ROE")

    def test_classic_order(self):
        eps = {2021: "1", 2022: "1.3", 2023: "1.5", 2024: "1.9", 2025: "2.5"}  # 15% in 2023
        self.assertEqual(build_a_score(contract(healthy(eps=eps)))["a_classic"]["result"], "FAIL_EPS_GROWTH")
        short = contract(healthy(eps={2024: "1", 2025: "2"}))
        self.assertEqual(build_a_score(short)["a_classic"]["result"], "INSUFFICIENT_HISTORY")


class RealDataTests(unittest.TestCase):
    def real(self, ticker, splits=SplitReconciliation("NO_RECENT_SPLITS", ())):
        return build_a_score(contract(real_facts(ticker), as_of=datetime(2026, 10, 2, tzinfo=UTC), splits=splits))

    def test_aapl(self):
        result = self.real("aapl")
        self.assertAlmostEqual(result["a_score_v1"]["normalized_score"], 44, delta=1)
        self.assertEqual(result["a_score_v1"]["class"], "POOR")
        self.assertEqual(result["a_classic"]["result"], "FAIL_EPS_GROWTH")
        self.assertIn("ROE_ABOVE_100_PCT", result["a_flags"])

    def test_nvda(self):
        result = self.real("nvda", SplitReconciliation("VERIFIED_ALREADY_ADJUSTED", NVDA_EVENTS))
        self.assertEqual(result["a_score_v1"]["normalized_score"], 100.0)
        self.assertEqual(result["a_score_v1"]["class"], "EXCEPTIONAL")
        self.assertEqual(result["a_classic"]["result"], "PASS")


if __name__ == "__main__":
    unittest.main()
