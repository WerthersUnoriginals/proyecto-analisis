"""Offline tests for the new-highs calculations (n-highs-v1)."""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from database.new_highs_v1 import NEW_HIGHS_VERSION, business_days_between, compute_highs
from database.prices_v3 import AdjustedBar

UTC = timezone.utc
OBSERVED = datetime(2026, 10, 3, tzinfo=UTC)


def sessions(count, *, end=date(2026, 10, 2)):
    """``count`` weekday dates ending at ``end``."""

    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def series(closes, *, highs=None, end=date(2026, 10, 2)):
    days = sessions(len(closes), end=end)
    highs = highs or closes
    return [
        AdjustedBar(
            bar_date=day, open=Decimal(str(close)), high=Decimal(str(high)), low=Decimal(str(close)),
            close=Decimal(str(close)), volume=Decimal(1000), basis_factor=Decimal(1), observed_at=OBSERVED,
        )
        for day, close, high in zip(days, closes, highs)
    ]


class BusinessDayTests(unittest.TestCase):
    def test_weekend_is_not_counted(self):
        self.assertEqual(business_days_between(date(2026, 10, 2), date(2026, 10, 5)), 1)  # Fri -> Mon
        self.assertEqual(business_days_between(date(2026, 10, 2), date(2026, 10, 2)), 0)


class HighsTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(NEW_HIGHS_VERSION, "n-highs-v1")

    def test_no_bars(self):
        result = compute_highs([], date(2026, 10, 2))
        self.assertEqual(result.status, "NO_PRICE_EVIDENCE")
        self.assertIsNone(result.high_52w)

    def test_recent_ipo_is_insufficient_history_not_unfavorable(self):
        result = compute_highs(series([10] * 200), date(2026, 10, 2))
        self.assertEqual(result.status, "INSUFFICIENT_HISTORY")
        self.assertEqual(result.sessions_available, 200)
        self.assertIsNone(result.pct_below_high_52w)

    def test_distance_to_52_week_high_uses_daily_high_and_last_close(self):
        closes = [100] * 251 + [90]
        highs = [100] * 100 + [120] + [100] * 150 + [91]
        result = compute_highs(series(closes, highs=highs), date(2026, 10, 2))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.high_52w, Decimal(120))
        self.assertEqual(result.close_last, Decimal(90))
        self.assertEqual(result.pct_below_high_52w, Decimal(25))
        self.assertEqual(result.sessions_since_high_52w, 151)
        self.assertFalse(result.new_high_recent)

    def test_only_the_last_252_sessions_count(self):
        closes = [500] + [100] * 252
        result = compute_highs(series(closes), date(2026, 10, 2))
        self.assertEqual(result.high_52w, Decimal(100))
        self.assertEqual(result.pct_below_high_52w, Decimal(0))

    def test_new_high_within_last_20_sessions(self):
        closes = [100] * 232 + [110] + [105] * 19
        result = compute_highs(series(closes), date(2026, 10, 2))
        self.assertEqual(result.sessions_since_high_52w, 19)
        self.assertTrue(result.new_high_recent)
        closes = [100] * 231 + [110] + [105] * 20
        self.assertFalse(compute_highs(series(closes), date(2026, 10, 2)).new_high_recent)

    def test_most_recent_session_at_the_high_counts(self):
        closes = [110] + [100] * 240 + [110] * 1 + [100] * 10
        result = compute_highs(series(closes), date(2026, 10, 2))
        self.assertEqual(result.sessions_since_high_52w, 10)

    def test_stale_prices(self):
        result = compute_highs(series([100] * 300, end=date(2026, 9, 21)), date(2026, 10, 2))
        self.assertEqual(result.status, "STALE_PRICES")
        self.assertEqual(result.high_52w, Decimal(100))  # values stay visible for audit

    def test_five_business_days_are_not_stale(self):
        result = compute_highs(series([100] * 300, end=date(2026, 9, 25)), date(2026, 10, 2))
        self.assertEqual(result.status, "OK")

    def test_gap_inside_the_window_requires_review(self):
        bars = series([100] * 300)
        bars = bars[:200] + bars[208:]  # eight missing sessions
        result = compute_highs(bars, date(2026, 10, 2))
        self.assertIn("PRICE_GAP", result.review_reasons)
        self.assertTrue(any(item.startswith("PRICE_GAP:") for item in result.diagnostics))

    def test_five_year_high_needs_coverage(self):
        result = compute_highs(series([100] * 300), date(2026, 10, 2))
        self.assertIsNone(result.high_5y)
        self.assertIn("HIGH_5Y_INSUFFICIENT_HISTORY", result.diagnostics)

    def test_five_year_high(self):
        closes = [400] * 10 + [100] * 140 + [200] + [100] * 1260  # ~5.4 years of sessions
        result = compute_highs(series(closes), date(2026, 10, 2))
        # Sessions older than five calendar years before the last bar are excluded.
        self.assertEqual(result.high_5y, Decimal(200))
        self.assertEqual(result.pct_below_high_5y, Decimal(50))


if __name__ == "__main__":
    unittest.main()
