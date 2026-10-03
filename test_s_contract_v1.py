"""Offline tests for the S input contract (s-input-contract-v1) and S Score v1."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.prices_v3 import PriceSeries
from database.s_contract_v1 import CONTRACT_VERSION, S_INPUT_KEYS, build_s_contract_v1
from database.split_basis import SplitEvent, SplitReconciliation
from database.supply_demand_v1 import compute_leverage, compute_share_supply, compute_volume_demand
from s_score_v1 import MODEL_VERSION, build_s_score
from test_new_highs_v1 import series
from test_supply_demand_v1 import shares_company, view

UTC = timezone.utc
AS_OF = datetime(2026, 3, 15, tzinfo=UTC)
NO_SPLITS = SplitReconciliation("NO_RECENT_SPLITS", ())


def healthy_facts():
    return shares_company({2021: "110", 2022: "105", 2023: "100", 2024: "98", 2025: "94.5"},
                          debt={2022: "60", 2025: "30"}, equity={2022: "100", 2025: "100"})


def demand_bars(up_volume=300, down_volume=100, end=date(2026, 3, 13)):
    bars = series([100] + [101, 100] * 25, end=end)
    volumes = [1] + [up_volume, down_volume] * 25
    return [bar.__class__(**{**bar.__dict__, "volume": Decimal(volume)}) for bar, volume in zip(bars, volumes)]


def contract(facts=None, *, bars=None, splits=NO_SPLITS, filer_status="DOMESTIC", as_of=AS_OF, series_review=()):
    annual = view(healthy_facts() if facts is None else facts)
    price_bars = demand_bars() if bars is None else bars
    price_series = PriceSeries(bars=tuple(price_bars), review_reasons=tuple(series_review))
    return build_s_contract_v1(
        annual, compute_share_supply(annual), compute_leverage(annual),
        compute_volume_demand(price_series.bars, as_of.date()), price_series, splits,
        split_status=splits.status, split_reasons=[], rejected_splits=(),
        company_id=1, as_of=as_of, filer_status=filer_status,
    )


class ContractTests(unittest.TestCase):
    def test_all_inputs_and_version(self):
        result = contract()
        self.assertEqual(CONTRACT_VERSION, "s-input-contract-v1")
        for key in S_INPUT_KEYS:
            self.assertIn(key, result)
            self.assertIn(key, result["s_input_contract"]["inputs"])
        self.assertEqual(result["shares_change_3y_pct"], -10.0)
        self.assertEqual(result["debt_to_equity_latest"], 0.3)
        self.assertEqual(result["up_down_volume_ratio_50d"], 3.0)
        self.assertEqual(result["s_data_integrity"], "VERIFIED")
        self.assertAlmostEqual(result["market_value_approx"], 100 * 94.5)

    def test_missing_debt_is_partial(self):
        facts = shares_company({2021: "110", 2022: "105", 2023: "100", 2024: "98", 2025: "95"})
        result = contract(facts)
        self.assertEqual(result["s_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")
        self.assertEqual(result["s_input_contract"]["inputs"]["debt_to_equity_latest"]["reasons"], ["NO_DEBT_EVIDENCE"])

    def test_non_meaningful_equity_requires_review(self):
        facts = shares_company({2021: "1", 2022: "1", 2023: "1", 2024: "1", 2025: "1"},
                               debt={2025: "10"}, equity={2025: "-5"})
        result = contract(facts)
        self.assertEqual(result["s_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("DEBT_TO_EQUITY_NOT_MEANINGFUL", result["integrity"]["diagnostics"])

    def test_stale_fiscal_year_requires_review(self):
        result = contract(as_of=datetime(2027, 6, 1, tzinfo=UTC), bars=demand_bars(end=date(2027, 5, 31)))
        self.assertEqual(result["s_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("STALE_LATEST_FISCAL_YEAR", result["integrity"]["diagnostics"])

    def test_stale_prices_require_review(self):
        result = contract(bars=demand_bars(end=date(2026, 2, 27)))
        self.assertEqual(result["s_data_integrity"], "REVIEW_REQUIRED")

    def test_leverage_from_an_older_year_is_flagged(self):
        facts = shares_company({2021: "110", 2022: "105", 2023: "100", 2024: "98", 2025: "95"},
                               debt={2024: "30"}, equity={2024: "100"})
        result = contract(facts)
        self.assertIn("LEVERAGE_NOT_LATEST_FISCAL_YEAR", result["integrity"]["diagnostics"])

    def test_split_events_are_informative(self):
        event = SplitEvent(date(2024, 6, 1), Decimal("4"), ("SEC", "YAHOO"))
        result = contract(splits=SplitReconciliation("VERIFIED_ALREADY_ADJUSTED", (event,)))
        self.assertEqual(result["split_events"], [{"date": "2024-06-01", "ratio": "4"}])
        self.assertEqual(result["s_data_integrity"], "VERIFIED")

    def test_foreign_filer_requires_review(self):
        self.assertEqual(contract(filer_status="FOREIGN_FILER_NOT_SUPPORTED")["s_data_integrity"], "REVIEW_REQUIRED")


class ScoreTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(MODEL_VERSION, "s-1.0-exp")

    def test_healthy_company(self):
        result = build_s_score(contract())
        score = result["s_score_v1"]
        components = score["components"]
        self.assertEqual(components["shares_3y"]["points"], 30.0)      # -10%
        self.assertEqual(components["shares_1y"]["points"], 10.0)     # -3.57%
        self.assertEqual(components["debt_level"]["points"], 13.0)     # 0.3
        self.assertEqual(components["debt_trend"]["points"], 10.0)     # -0.3
        self.assertEqual(components["volume_demand"]["points"], 35.0)  # 3.0
        self.assertEqual(result["s_classic"]["result"], "PASS")
        self.assertEqual(score["usability"], "S_SCORE_USABLE")

    def test_classic_order(self):
        base = {"up_down_volume_ratio_50d": 1.2, "shares_change_3y_pct": 0.0, "debt_to_equity_change": 0.0}
        classic = lambda **kw: build_s_score({**base, **kw})["s_classic"]["result"]
        self.assertEqual(classic(), "PASS")
        self.assertEqual(classic(up_down_volume_ratio_50d=None), "INSUFFICIENT_DATA")
        self.assertEqual(classic(up_down_volume_ratio_50d=0.99), "FAIL_DISTRIBUTION")
        self.assertEqual(classic(shares_change_3y_pct=5.01), "FAIL_DILUTION")
        self.assertEqual(classic(debt_to_equity_change=0.26), "FAIL_DEBT_RISING")
        self.assertEqual(classic(debt_to_equity_change=None), "PASS")  # unknown trend is not a failure

    def test_missing_debt_renormalizes_and_reviews(self):
        facts = shares_company({2021: "110", 2022: "105", 2023: "100", 2024: "98", 2025: "95"})
        score = build_s_score(contract(facts))["s_score_v1"]
        self.assertEqual(score["available_points"], 75.0)
        self.assertEqual((score["status"], score["usability"]), ("PARTIAL_SCORE", "S_SCORE_REVIEW"))

    def test_flags(self):
        event = SplitEvent(date(2024, 6, 1), Decimal("4"), ("SEC", "YAHOO"))
        flags = build_s_score(contract(splits=SplitReconciliation("VERIFIED_ALREADY_ADJUSTED", (event,))))["s_flags"]
        self.assertIn("SHARE_BUYBACK_3Y", flags)
        self.assertIn("SPLITS_IN_WINDOW:1", flags)


class RunnerTests(unittest.TestCase):
    def test_runner_wires_evidence(self):
        from unittest import mock

        from database import evidence_v3
        from database.prices_v3 import PriceBar
        from database.s_v3_runner import evaluate_s

        raw = [PriceBar(bar_date=bar.bar_date, open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                        adj_close=bar.close, volume=int(bar.volume), currency="USD", observed_at=AS_OF)
               for bar in demand_bars()]
        with mock.patch.object(evidence_v3, "load_sec_facts", return_value=healthy_facts()), \
                mock.patch.object(evidence_v3, "load_price_bars", return_value=raw), \
                mock.patch.object(evidence_v3, "load_filing_forms", return_value=[("10-K", date(2026, 2, 20))]), \
                mock.patch.object(evidence_v3, "load_registrant_links", return_value=[]):
            result = evaluate_s(1, AS_OF, capture_loader=lambda company, as_of: (None, []))
        contract = result["contract"]
        self.assertEqual(contract["shares_change_3y_pct"], -10.0)
        self.assertEqual(contract["up_down_volume_ratio_50d"], 3.0)
        # No visible split capture: unknown split status fails closed, as in A and N.
        self.assertEqual(contract["s_data_integrity"], "REVIEW_REQUIRED")
        self.assertEqual(result["score"]["s_classic"]["result"], "PASS")


if __name__ == "__main__":
    unittest.main()
