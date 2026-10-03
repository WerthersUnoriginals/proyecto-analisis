"""Offline tests for market direction (m-market-direction-v1)."""

import unittest
from datetime import date, timedelta
from decimal import Decimal

from database.market_direction_v1 import (
    MARKET_DIRECTION_VERSION,
    analyze_index,
    breadth_above_ma50,
    combine_states,
    distribution_flags,
)
from database.prices_v3 import AdjustedBar
from test_new_highs_v1 import sessions

END = date(2026, 10, 2)


def bars(closes, volumes=None):
    volumes = volumes or [1000] * len(closes)
    days = sessions(len(closes), end=END)
    return [AdjustedBar(day, Decimal(str(c)), Decimal(str(c)), Decimal(str(c)), Decimal(str(c)), Decimal(v),
                        Decimal(1), None) for day, c, v in zip(days, closes, volumes)]


def uptrend(count, start=100.0, daily=0.001):
    return [start * (1 + daily) ** day for day in range(count)]


class DistributionTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(MARKET_DIRECTION_VERSION, "m-market-direction-v1")

    def test_down_on_higher_volume_is_distribution(self):
        closes = [100, 99.7, 99.6, 99.5]
        volumes = [1000, 1100, 900, 1200]
        # day 1: -0.3% on more volume -> yes; day 2: -0.1% -> no; day 3: -0.1% on more volume -> no.
        self.assertEqual(distribution_flags(bars(closes, volumes)), [False, True, False, False])

    def test_active_window_and_5_pct_expiry(self):
        closes = uptrend(530)
        volumes = [1000] * 530
        closes[500], volumes[500] = closes[499] * 0.99, 2000          # distribution day, 29 sessions ago
        closes[520], volumes[520] = closes[519] * 0.99, 2000          # distribution day, 9 sessions ago
        result = analyze_index(bars(closes, volumes))
        self.assertEqual(result.active_distribution_days, 1)            # the older one is out of 25 sessions
        expired = closes[:]
        for index in range(521, 530):
            expired[index] = expired[520] * 1.06                         # 6% above the distribution close
        self.assertEqual(analyze_index(bars(expired, volumes)).active_distribution_days, 0)


def correction_then_rally(ftd_day=4, ftd_gain=0.015, ftd_volume=2000, undercut=False):
    closes = uptrend(560)                                  # long uptrend
    peak = closes[-1]
    closes += [peak * (1 - 0.012 * step) for step in range(1, 11)]   # -12%: correction
    low = closes[-1]
    rally = [low * 1.002, low * 1.004, low * 1.006]        # days 1-3
    if undercut:
        rally[1] = low * 0.99                                # new low resets the attempt
    closes += rally
    for _ in range(ftd_day - 4):
        closes.append(closes[-1] * 1.001)
    closes.append(closes[-1] * (1 + ftd_gain))
    volumes = [1000] * len(closes)
    volumes[-1] = ftd_volume
    closes += [closes[-1] * 1.001] * 5
    volumes += [1000] * 5
    return closes, volumes


class StateMachineTests(unittest.TestCase):
    def test_long_uptrend_is_confirmed(self):
        result = analyze_index(bars(uptrend(600)))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.state, "CONFIRMED_UPTREND")
        self.assertGreater(result.pct_vs_ma50, 0)
        self.assertGreater(result.pct_vs_ma200, 0)

    def test_drop_of_8_pct_is_a_correction(self):
        closes = uptrend(600) + [uptrend(600)[-1] * 0.9] * 3
        result = analyze_index(bars(closes))
        self.assertEqual(result.state, "CORRECTION")

    def test_follow_through_day_confirms_a_new_uptrend(self):
        closes, volumes = correction_then_rally()
        result = analyze_index(bars(closes, volumes))
        self.assertEqual(result.state, "CONFIRMED_UPTREND")
        self.assertEqual(result.last_follow_through_day, sessions(len(closes), end=END)[-6])

    def test_follow_through_day_clears_earlier_distribution_days(self):
        closes, volumes = correction_then_rally()
        for index in range(560, 570):                      # every correction day on rising volume
            volumes[index] = 1000 + 100 * (index - 559)
        self.assertGreaterEqual(sum(distribution_flags(bars(closes, volumes))[555:]), 6)
        result = analyze_index(bars(closes, volumes))
        # Without the reset, ten distribution days inside 25 sessions would end the new uptrend at once.
        self.assertEqual(result.state, "CONFIRMED_UPTREND")
        self.assertEqual(result.active_distribution_days, 0)

    def test_strong_day_before_day_4_does_not_count(self):
        closes, volumes = correction_then_rally()
        closes, volumes = closes[:-9], volumes[:-9]          # cut before day 4
        closes.append(closes[-1] * 1.02)
        volumes.append(3000)
        self.assertEqual(analyze_index(bars(closes, volumes)).state, "CORRECTION")

    def test_follow_through_needs_volume_and_gain(self):
        closes, volumes = correction_then_rally(ftd_volume=900)
        self.assertEqual(analyze_index(bars(closes, volumes)).state, "CORRECTION")
        closes, volumes = correction_then_rally(ftd_gain=0.01)
        self.assertEqual(analyze_index(bars(closes, volumes)).state, "CORRECTION")

    def test_undercutting_the_low_restarts_the_count(self):
        closes, volumes = correction_then_rally(undercut=True)
        result = analyze_index(bars(closes, volumes))
        self.assertEqual(result.state, "CORRECTION")
        self.assertGreater(result.rally_day, 0)

    def test_distribution_days_put_the_uptrend_under_pressure(self):
        closes = uptrend(600)
        volumes = [1000] * 600
        for offset in (20, 15, 10, 5):                      # four distribution days in 25 sessions
            index = 600 - offset
            closes[index] = closes[index - 1] * 0.997
            volumes[index] = 2000
            for after in range(index + 1, 600):
                closes[after] = closes[after - 1] * 1.0005
        result = analyze_index(bars(closes, volumes))
        self.assertEqual(result.active_distribution_days, 4)
        self.assertEqual(result.state, "UPTREND_UNDER_PRESSURE")

    def test_many_distribution_days_without_price_damage_stay_under_pressure(self):
        closes = uptrend(600)
        volumes = [1000] * 600
        for offset in (24, 21, 18, 15, 12, 9, 6, 3):          # eight distribution days, no 8% drawdown
            index = 600 - offset
            closes[index] = closes[index - 1] * 0.997
            volumes[index] = 2000
            for after in range(index + 1, 600):
                closes[after] = closes[after - 1] * 1.0002
        result = analyze_index(bars(closes, volumes))
        self.assertEqual(result.active_distribution_days, 8)
        self.assertEqual(result.state, "UPTREND_UNDER_PRESSURE")

    def test_insufficient_history(self):
        self.assertEqual(analyze_index(bars(uptrend(450))).status, "INSUFFICIENT_HISTORY")

    def test_combined_state_is_the_worse(self):
        self.assertEqual(combine_states(["CONFIRMED_UPTREND", "UPTREND_UNDER_PRESSURE"]), "UPTREND_UNDER_PRESSURE")
        self.assertEqual(combine_states(["CORRECTION", "CONFIRMED_UPTREND"]), "CORRECTION")
        self.assertIsNone(combine_states([None, "CONFIRMED_UPTREND"]))


class BreadthTests(unittest.TestCase):
    def test_share_above_ma50(self):
        rising = bars(uptrend(60))
        falling = bars(list(reversed(uptrend(60))))
        short = bars(uptrend(30))
        pct, members = breadth_above_ma50([rising, rising, falling, short], END)
        self.assertEqual(members, 3)
        self.assertAlmostEqual(float(pct), 200 / 3)


if __name__ == "__main__":
    unittest.main()
