"""Offline tests for the L input contract (l-input-contract-v1) and L Score v1."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.l_contract_v1 import CONTRACT_VERSION, L_INPUT_KEYS, build_l_contract_v1
from database.prices_v3 import PriceSeries
from database.relative_strength_v1 import GroupStrength, RsLine, WeightedScore
from database.split_basis import SplitReconciliation
from l_score_v1 import MODEL_VERSION, build_l_score

UTC = timezone.utc
AS_OF = datetime(2026, 10, 3, tzinfo=UTC)
NO_SPLITS = SplitReconciliation("NO_RECENT_SPLITS", ())


def contract(*, rating=92, score=Decimal("1.35"), group=None, line=None, universe_scored=480, series_review=(),
             filer_status="DOMESTIC", split_status="NO_RECENT_SPLITS", score_status="OK", sic="3674"):
    group = group or GroupStrength("OK", "36", 25, Decimal(85), Decimal(90), 60)
    line = line or RsLine("OK", 252, Decimal(2), True, False)
    return build_l_contract_v1(
        WeightedScore(score_status, score if score_status == "OK" else None, date(2026, 10, 2)),
        rating if score_status == "OK" else None, group, line, PriceSeries(bars=(), review_reasons=tuple(series_review)),
        NO_SPLITS, split_status=split_status, split_reasons=[], rejected_splits=(),
        universe_as_of=date(2026, 10, 1), universe_scored=universe_scored, universe_excluded=3,
        sic=sic, sic_description="Semiconductors", company_id=1, as_of=AS_OF, filer_status=filer_status,
        price_pct_below_high_52w=Decimal(8),
    )


class ContractTests(unittest.TestCase):
    def test_all_inputs_and_version(self):
        result = contract()
        self.assertEqual(CONTRACT_VERSION, "l-input-contract-v1")
        for key in L_INPUT_KEYS:
            self.assertIn(key, result)
            self.assertIn(key, result["l_input_contract"]["inputs"])
        self.assertEqual(result["rs_rating"], 92)
        self.assertAlmostEqual(result["weighted_return_pct"], 35.0)
        self.assertEqual((result["group_key"], result["group_rank_pct"]), ("36", 85.0))
        self.assertEqual(result["l_data_integrity"], "VERIFIED")

    def test_incomplete_universe_requires_review(self):
        result = contract(universe_scored=300)
        self.assertEqual(result["l_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("UNIVERSE_INCOMPLETE", result["integrity"]["diagnostics"])

    def test_unranked_group_is_partial(self):
        result = contract(group=GroupStrength("GROUP_NOT_RANKED", "10", 1))
        self.assertEqual(result["l_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")
        self.assertIsNone(result["group_rank_pct"])

    def test_stale_or_review_prices(self):
        self.assertEqual(contract(score_status="STALE_PRICES")["l_data_integrity"], "REVIEW_REQUIRED")
        self.assertEqual(contract(series_review=["PRICE_BASIS_CONFLICT"])["l_data_integrity"], "REVIEW_REQUIRED")

    def test_insufficient_history_is_partial(self):
        result = contract(score_status="INSUFFICIENT_HISTORY")
        self.assertIsNone(result["rs_rating"])
        self.assertEqual(result["l_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")

    def test_rs_line_leading_price_flag(self):
        result = contract(line=RsLine("OK", 252, Decimal(0), True, True))
        self.assertIn("RS_LINE_HIGH_BEFORE_PRICE", result["integrity"]["diagnostics"])

    def test_foreign_filer_requires_review(self):
        self.assertEqual(contract(filer_status="FOREIGN_FILER_NOT_SUPPORTED")["l_data_integrity"], "REVIEW_REQUIRED")


class ScoreTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(MODEL_VERSION, "l-1.0-exp")

    def test_components(self):
        score = build_l_score(contract())["l_score_v1"]
        points = {name: item["points"] for name, item in score["components"].items()}
        self.assertAlmostEqual(points["rs_rating"], 46 + (2 / 5) * 4)       # 92
        self.assertAlmostEqual(points["group_rank"], 16 + (5 / 10) * 4)     # 85
        self.assertAlmostEqual(points["rank_in_group"], 8 + (10 / 20) * 2)  # 90
        self.assertAlmostEqual(points["rs_line_distance"], 14 - (2 / 5) * 4)  # 2%
        self.assertEqual(points["rs_line_new_high"], 6.0)

    def test_classic(self):
        self.assertEqual(build_l_score(contract(rating=80))["l_classic"]["result"], "PASS")
        self.assertEqual(build_l_score(contract(rating=79))["l_classic"]["result"], "FAIL_LAGGARD")
        self.assertEqual(build_l_score(contract(score_status="INSUFFICIENT_HISTORY"))["l_classic"]["result"],
                         "INSUFFICIENT_DATA")

    def test_flags(self):
        flags = build_l_score(contract(line=RsLine("OK", 252, Decimal(0), True, True)))["l_flags"]
        self.assertIn("RS_LINE_HIGH_BEFORE_PRICE", flags)
        self.assertIn("RS_LINE_NEW_HIGH", flags)


class RunnerTests(unittest.TestCase):
    def test_runner_with_injected_universe(self):
        from unittest import mock

        from database import evidence_v3
        from database.l_v3_runner import UniverseContext, evaluate_l
        from database.prices_v3 import PriceBar
        from database.relative_strength_v1 import UniverseMember
        from test_new_highs_v1 import series

        stock = series([100 * 1.002 ** day for day in range(300)])
        market = series([100 * 1.001 ** day for day in range(300)])
        members = tuple(UniverseMember(f"M{index}", "3674" if index % 2 else "2834", Decimal(1) + Decimal(index) / 1000)
                        for index in range(450))
        context = UniverseContext(AS_OF, date(2026, 10, 1), members, 5, tuple(market))
        raw = [PriceBar(bar_date=bar.bar_date, open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                        adj_close=bar.close, volume=1, currency="USD", observed_at=AS_OF) for bar in stock]
        with (
            mock.patch.object(evidence_v3, "load_sec_facts", return_value=[]),
            mock.patch.object(evidence_v3, "load_price_bars", return_value=raw),
            mock.patch.object(evidence_v3, "load_profiles", return_value={1: ("3674", "Semiconductors")}),
            mock.patch.object(evidence_v3, "load_filing_forms", return_value=[("10-K", date(2026, 2, 1))]),
        ):
            result = evaluate_l(1, "X", AS_OF, context=context, capture_loader=lambda company, as_of: (None, []))
        contract = result["contract"]
        # Score 1.3336 beats 334 of the 450 members (1.000..1.449): 1 + floor(99 * 334/450) = 74.
        self.assertEqual(contract["rs_rating"], 74)
        self.assertEqual(contract["group_key"], "36")
        self.assertTrue(contract["rs_line_new_high_recent"])
        self.assertEqual(contract["universe_scored"], 450)


if __name__ == "__main__":
    unittest.main()
