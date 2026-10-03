"""Offline tests for the M input contract (m-input-contract-v1), M Score v1 and the benchmark registry."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.m_contract_v1 import CONTRACT_VERSION, M_INPUT_KEYS, build_m_contract_v1
from database.market_direction_v1 import IndexDirection
from database.providers_v3 import ProviderError
from m_score_v1 import MODEL_VERSION, build_m_score

UTC = timezone.utc
AS_OF = datetime(2026, 10, 3, tzinfo=UTC)


def index(state="CONFIRMED_UPTREND", distribution=1, ma50="3", ma200="9", status="OK", ftd=None):
    return IndexDirection(status, state, date(2026, 8, 1), ftd, distribution, (), None, Decimal(ma50), Decimal(ma200),
                          date(2026, 10, 2))


def contract(spy=None, qqq=None, *, breadth=Decimal(65), members=497, review=None):
    return build_m_contract_v1({"SPY": spy or index(), "QQQ": qqq or index()},
                               series_review=review or {"SPY": (), "QQQ": ()}, breadth_pct=breadth,
                               breadth_members=members, as_of=AS_OF)


class ContractTests(unittest.TestCase):
    def test_all_inputs_and_version(self):
        result = contract()
        self.assertEqual(CONTRACT_VERSION, "m-input-contract-v1")
        for key in M_INPUT_KEYS:
            self.assertIn(key, result)
            self.assertIn(key, result["m_input_contract"]["inputs"])
        self.assertEqual(result["market_state"], "CONFIRMED_UPTREND")
        self.assertEqual(result["m_data_integrity"], "VERIFIED")

    def test_worse_index_drives_the_market(self):
        result = contract(qqq=index("UPTREND_UNDER_PRESSURE", distribution=4))
        self.assertEqual(result["market_state"], "UPTREND_UNDER_PRESSURE")
        self.assertEqual(result["state_by_index"], {"SPY": "CONFIRMED_UPTREND", "QQQ": "UPTREND_UNDER_PRESSURE"})

    def test_benchmark_problems_require_review(self):
        self.assertEqual(contract(spy=index(status="STALE_PRICES"))["m_data_integrity"], "REVIEW_REQUIRED")
        self.assertEqual(contract(review={"SPY": ("PRICE_BASIS_CONFLICT",), "QQQ": ()})["m_data_integrity"],
                         "REVIEW_REQUIRED")
        missing = contract(qqq=IndexDirection("INSUFFICIENT_HISTORY"))
        self.assertIsNone(missing["market_state"])
        self.assertEqual(missing["m_data_integrity"], "REVIEW_REQUIRED")

    def test_small_breadth_universe_is_partial(self):
        self.assertEqual(contract(members=300)["m_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")


class ScoreTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(MODEL_VERSION, "m-1.0-exp")

    def test_confirmed_uptrend(self):
        result = build_m_score(contract())
        points = {name: item["points"] for name, item in result["m_score_v1"]["components"].items()}
        self.assertEqual(points["market_state"], 50.0)
        self.assertAlmostEqual(points["distribution_days"], 13.5)
        self.assertEqual(points["moving_averages"], 20.0)
        self.assertAlmostEqual(points["breadth"], 13.5)
        self.assertEqual(result["m_classic"]["result"], "PASS")

    def test_correction(self):
        weak = index("CORRECTION", distribution=6, ma50="-4", ma200="-1")
        result = build_m_score(contract(spy=weak, qqq=weak, breadth=Decimal(15)))
        self.assertEqual(result["m_score_v1"]["normalized_score"], 0.0)
        self.assertEqual(result["m_classic"]["result"], "FAIL_MARKET_IN_CORRECTION")

    def test_follow_through_flag(self):
        result = build_m_score(contract(spy=index(ftd=date(2026, 5, 4))))
        self.assertEqual(result["m_flags"], ["FOLLOW_THROUGH_DAY:2026-05-04"])


class BenchmarkTests(unittest.TestCase):
    def test_registry_identity_is_verified_against_sec(self):
        from database.market_v1 import benchmark_identity

        payload = {"cik": "0001067839", "name": "INVESCO QQQ TRUST, SERIES 1", "tickers": [], "sic": ""}
        identity = benchmark_identity("QQQ", getter=lambda url: payload)
        self.assertEqual((identity.ticker, identity.cik), ("QQQ", "0001067839"))
        with self.assertRaises(ProviderError) as error:
            benchmark_identity("QQQ", getter=lambda url: {**payload, "name": "SOMETHING ELSE"})
        self.assertEqual(error.exception.code, "BENCHMARK_NAME_MISMATCH")


if __name__ == "__main__":
    unittest.main()
