"""Offline tests for the Entry Phase Score v2 port (entry-phase-v2)."""

import math
import unittest
from datetime import date
from decimal import Decimal

from database.entry_phase_v2 import (
    ENTRY_PHASE_VERSION,
    MIN_BARS,
    compute_entry_phase,
    correlation_with_index,
    highest,
    linreg,
    percentrank,
    rma,
    rsi,
    sma,
    stdev,
    true_range,
)
from database.prices_v3 import AdjustedBar
from test_new_highs_v1 import sessions

END = date(2026, 10, 2)


def bars(closes, highs=None, lows=None, volumes=None):
    days = sessions(len(closes), end=END)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    volumes = volumes or [1000] * len(closes)
    return [AdjustedBar(day, Decimal(str(c)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)), Decimal(v),
                        Decimal(1), None) for day, c, h, l, v in zip(days, closes, highs, lows, volumes)]


class BuiltinTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(ENTRY_PHASE_VERSION, "entry-phase-v2")
        self.assertEqual(MIN_BARS, 430)

    def test_sma_highest(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        self.assertEqual(highest([1, 5, 2, 3], 2), [None, 5, 5, 3])

    def test_percentrank_excludes_the_current_value(self):
        # previous 4 values [1, 2, 3, 4]; current 3 -> 3 of them <= 3 -> 75%.
        self.assertEqual(percentrank([1, 2, 3, 4, 3], 4)[-1], 75.0)
        self.assertIsNone(percentrank([1, 2, 3], 4)[-1])

    def test_rma_is_seeded_with_the_sma(self):
        self.assertEqual(rma([2, 4, 6, 8], 2), [None, 3.0, 4.5, 6.25])

    def test_true_range_uses_the_previous_close(self):
        self.assertEqual(true_range([10, 12], [9, 11], [9.5, 11.5]), [1, 2.5])

    def test_rsi(self):
        self.assertEqual(rsi([1, 2, 3, 4], 2)[-1], 100.0)            # no losses
        self.assertEqual(rsi([4, 3, 2, 1], 2)[-1], 0.0)              # no gains
        values = rsi([1, 2, 1, 2, 1], 2)
        # Wilder averages over the last changes: gains 0.375, losses 0.625.
        self.assertAlmostEqual(values[-1], 100 - 100 / (1 + 0.375 / 0.625))

    def test_regression_helpers(self):
        self.assertAlmostEqual(linreg([1, 2, 3, 4], 0), 4.0)
        self.assertAlmostEqual(linreg([1, 2, 3, 4], 1), 3.0)
        self.assertAlmostEqual(correlation_with_index([1, 2, 3, 4]), 1.0)
        self.assertIsNone(correlation_with_index([2, 2, 2]))
        self.assertAlmostEqual(stdev([1, 3]), 1.0)


def trend(count, start=100.0, daily=0.001):
    return [start * (1 + daily) ** day for day in range(count)]


class IndicatorTests(unittest.TestCase):
    def test_insufficient_history(self):
        self.assertEqual(compute_entry_phase(bars(trend(MIN_BARS - 1)), END).status, "INSUFFICIENT_HISTORY")

    def test_steady_uptrend_is_a_trend_channel_near_its_high(self):
        result = compute_entry_phase(bars(trend(600)), END)
        self.assertEqual(result.status, "OK")
        self.assertTrue(result.layer1_ok)
        self.assertEqual(result.setup_type, "TREND_CHANNEL")
        self.assertTrue(result.conditions["range_position"])
        self.assertTrue(result.conditions["trigger_distance"])     # the high is 1% above the close
        self.assertEqual(result.momentum, "CONFIRMED")
        self.assertEqual(len(result.score_history), 10)

    def test_downtrend_fails_layer_1_and_scores_zero(self):
        result = compute_entry_phase(bars(list(reversed(trend(600)))), END)
        self.assertFalse(result.layer1_ok)
        self.assertEqual(result.score_final, 0)

    def test_tight_base_after_an_advance(self):
        # Advance, then a wide swing, then a tight 42-session base near the high with shrinking ranges.
        closes = trend(450, daily=0.002)
        peak = closes[-1]
        closes += [peak * (1 + 0.05 * math.sin(day / 3)) for day in range(60)]
        closes += [peak * 1.0 + (day % 2) * 0.1 for day in range(60)]
        highs = [c * 1.01 for c in closes[:-60]] + [c + 0.3 for c in closes[-60:]]
        lows = [c * 0.99 for c in closes[:-60]] + [c - 0.3 for c in closes[-60:]]
        result = compute_entry_phase(bars(closes, highs, lows), END)
        self.assertTrue(result.layer1_ok)
        self.assertTrue(result.conditions["amplitude_percentile"])
        self.assertTrue(result.conditions["atr_contraction"] or result.values["atr"] <= result.values["atr_past"])
        self.assertIn(result.setup_type, ("COMPRESSION_BASE", "TREND_CHANNEL"))

    def test_volume_collapse_fails_condition_4(self):
        volumes = [1000] * 595 + [100] * 5
        result = compute_entry_phase(bars(trend(600), volumes=volumes), END)
        self.assertFalse(result.conditions["volume_sustained"])

    def test_stale_prices(self):
        self.assertEqual(compute_entry_phase(bars(trend(600)), date(2026, 10, 20)).status, "STALE_PRICES")


class ContractTests(unittest.TestCase):
    def contract(self, result, review=()):
        from datetime import datetime, timezone

        from database.prices_v3 import PriceSeries
        from database.t_contract_v1 import CONTRACT_VERSION, build_t_contract_v1

        self.assertEqual(CONTRACT_VERSION, "t-input-contract-v1")
        return build_t_contract_v1(result, PriceSeries(bars=(), review_reasons=tuple(review)), company_id=1,
                                   as_of=datetime(2026, 10, 3, tzinfo=timezone.utc))

    def test_ready_needs_layer1_and_score_4(self):
        from database.entry_phase_v2 import EntryPhase

        ready = self.contract(EntryPhase("OK", END, True, {}, 4, 4, "COMPRESSION_BASE", "N/A"))
        self.assertTrue(ready["entry_phase_ready"])
        self.assertEqual(ready["t_data_integrity"], "VERIFIED")
        self.assertFalse(self.contract(EntryPhase("OK", END, False, {}, 5, 0, "X", "N/A"))["entry_phase_ready"])
        self.assertFalse(self.contract(EntryPhase("OK", END, True, {}, 3, 3, "X", "N/A"))["entry_phase_ready"])

    def test_review_and_partial(self):
        from database.entry_phase_v2 import EntryPhase

        reviewed = self.contract(EntryPhase("OK", END, True, {}, 5, 5, "X", "N/A"), review=["PRICE_BASIS_CONFLICT"])
        self.assertEqual(reviewed["t_data_integrity"], "REVIEW_REQUIRED")
        self.assertFalse(reviewed["entry_phase_ready"])
        self.assertEqual(self.contract(EntryPhase("INSUFFICIENT_HISTORY"))["t_data_integrity"],
                         "VERIFIED_WITH_PARTIAL_CORE_DATA")


if __name__ == "__main__":
    unittest.main()
