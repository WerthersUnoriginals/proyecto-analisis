"""Offline tests for daily price evidence and the price series (price-series-v1)."""

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from database.prices_v3 import (
    PRICE_SERIES_VERSION,
    PriceBar,
    bars_from_rows,
    build_price_series,
    is_final_bar,
)
from database.split_basis import SplitEvent

UTC = timezone.utc
NY = "America/New_York"
FIXTURE = Path(__file__).parent / "fixtures" / "yahoo_bars_nvda_2024-06_split_2026-10-03.json"
NVDA_SPLIT = SplitEvent(date(2024, 6, 10), Decimal("10"), ("SEC", "YAHOO"))


def fixture_rows():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        (date.fromisoformat(row["date"]), {
            "Open": float(row["open"]), "High": float(row["high"]), "Low": float(row["low"]),
            "Close": float(row["close"]), "Adj Close": float(row["adj_close"]), "Volume": row["volume"],
        })
        for row in payload["bars"]
    ]


def fixture_bars(observed_at, *, scale=Decimal(1)):
    """Fixture bars as observed at ``observed_at``; ``scale`` re-creates a pre-split basis."""

    bars, _ = bars_from_rows(fixture_rows(), observed_at=observed_at, exchange_timezone=NY, currency="USD")
    return [
        PriceBar(
            bar_date=bar.bar_date, open=bar.open * scale, high=bar.high * scale, low=bar.low * scale,
            close=bar.close * scale, adj_close=bar.adj_close * scale, volume=int(bar.volume / scale),
            currency="USD", observed_at=observed_at,
        )
        for bar in bars
        if bar.bar_date < observed_at.date()
    ]


def bar(day, close, *, observed_at=None, high=None, low=None, open_=None, currency="USD"):
    close = Decimal(str(close))
    return PriceBar(
        bar_date=day, open=Decimal(str(open_)) if open_ is not None else close,
        high=Decimal(str(high)) if high is not None else close,
        low=Decimal(str(low)) if low is not None else close,
        close=close, adj_close=close, volume=1000, currency=currency,
        observed_at=observed_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


class FinalityTests(unittest.TestCase):
    def test_previous_session_is_final(self):
        self.assertTrue(is_final_bar(date(2026, 10, 1), datetime(2026, 10, 2, 14, tzinfo=UTC), NY))

    def test_same_day_bar_during_session_is_partial(self):
        # 14:00 UTC = 10:00 New York.
        self.assertFalse(is_final_bar(date(2026, 10, 2), datetime(2026, 10, 2, 14, tzinfo=UTC), NY))

    def test_same_day_bar_after_20h_exchange_time_is_final(self):
        # 00:30 UTC on Oct 3 = 20:30 New York on Oct 2.
        self.assertTrue(is_final_bar(date(2026, 10, 2), datetime(2026, 10, 3, 0, 30, tzinfo=UTC), NY))

    def test_observation_date_uses_exchange_timezone(self):
        # 02:00 UTC on Oct 3 is still Oct 2 in New York; an Oct 3 bar cannot exist yet.
        self.assertFalse(is_final_bar(date(2026, 10, 3), datetime(2026, 10, 3, 2, tzinfo=UTC), NY))


class RowParsingTests(unittest.TestCase):
    OBSERVED = datetime(2026, 10, 2, 14, tzinfo=UTC)

    def test_literal_values_rounded_to_micro_units(self):
        bars, meta = bars_from_rows(fixture_rows(), observed_at=self.OBSERVED, exchange_timezone=NY, currency="USD")
        self.assertEqual(len(bars), 29)
        first = bars[0]
        self.assertEqual(first.bar_date, date(2024, 5, 20))
        self.assertEqual(first.high, Decimal("95.199997"))
        self.assertEqual(first.volume, 318764000)
        self.assertEqual(meta, {"partial_bars": 0, "rejected_bars": []})

    def test_partial_bar_is_dropped_and_counted(self):
        rows = [(date(2026, 10, 1), {"Open": 10, "High": 11, "Low": 9, "Close": 10, "Adj Close": 10, "Volume": 5}),
                (date(2026, 10, 2), {"Open": 10, "High": 11, "Low": 9, "Close": 10, "Adj Close": 10, "Volume": 5})]
        bars, meta = bars_from_rows(rows, observed_at=self.OBSERVED, exchange_timezone=NY, currency="USD")
        self.assertEqual([item.bar_date for item in bars], [date(2026, 10, 1)])
        self.assertEqual(meta["partial_bars"], 1)

    def test_invalid_rows_are_rejected_with_reason(self):
        rows = [
            (date(2026, 9, 28), {"Open": float("nan"), "High": 11, "Low": 9, "Close": 10, "Adj Close": 10, "Volume": 5}),
            (date(2026, 9, 29), {"Open": 10, "High": 11, "Low": 0, "Close": 10, "Adj Close": 10, "Volume": 5}),
            (date(2026, 9, 30), {"Open": 10, "High": 9, "Low": 8, "Close": 10, "Adj Close": 10, "Volume": 5}),
            (date(2026, 10, 1), {"Open": 10, "High": 11, "Low": 9, "Close": 10, "Adj Close": 10, "Volume": -1}),
        ]
        bars, meta = bars_from_rows(rows, observed_at=self.OBSERVED, exchange_timezone=NY, currency="USD")
        self.assertEqual(bars, [])
        self.assertEqual([item["reason"] for item in meta["rejected_bars"]], [
            "NAN_VALUE", "NON_POSITIVE_PRICE", "OHLC_INCONSISTENT", "NEGATIVE_VOLUME",
        ])


class SeriesTests(unittest.TestCase):
    POST_SPLIT = datetime(2026, 10, 2, 14, tzinfo=UTC)
    PRE_SPLIT = datetime(2024, 6, 1, 14, tzinfo=UTC)

    def test_post_split_observation_needs_no_factor(self):
        series = build_price_series(fixture_bars(self.POST_SPLIT), split_events=(NVDA_SPLIT,), as_of=self.POST_SPLIT)
        self.assertEqual(series.version, PRICE_SERIES_VERSION)
        self.assertEqual(series.review_reasons, ())
        june_5 = next(item for item in series.bars if item.bar_date == date(2024, 6, 5))
        self.assertEqual(june_5.basis_factor, 1)
        self.assertEqual(june_5.close, Decimal("122.440002"))

    def test_pre_split_observation_converts_to_as_of_basis(self):
        pre = fixture_bars(self.PRE_SPLIT, scale=Decimal(10))
        as_of = datetime(2024, 7, 1, tzinfo=UTC)
        series = build_price_series(pre, split_events=(NVDA_SPLIT,), as_of=as_of)
        self.assertEqual(series.review_reasons, ())
        last = series.bars[-1]
        self.assertEqual(last.bar_date, date(2024, 5, 31))
        self.assertEqual(last.basis_factor, 10)
        reference = next(item for item in fixture_bars(self.POST_SPLIT) if item.bar_date == date(2024, 5, 31))
        self.assertEqual(last.close, reference.close)
        self.assertLess(abs(last.volume - reference.volume), 10)  # fixture volume was divided by 10

    def test_pre_split_as_of_keeps_pre_split_basis(self):
        pre = fixture_bars(self.PRE_SPLIT, scale=Decimal(10))
        series = build_price_series(pre, split_events=(NVDA_SPLIT,), as_of=datetime(2024, 6, 5, tzinfo=UTC))
        self.assertEqual(series.bars[-1].basis_factor, 1)
        self.assertGreater(series.bars[-1].close, 1000)

    def test_latest_observation_of_each_bar_wins_and_matching_observations_agree(self):
        pre = fixture_bars(self.PRE_SPLIT, scale=Decimal(10))
        post = fixture_bars(self.POST_SPLIT)
        series = build_price_series(pre + post, split_events=(NVDA_SPLIT,), as_of=self.POST_SPLIT)
        self.assertEqual(series.review_reasons, ())
        self.assertTrue(all(item.observed_at == self.POST_SPLIT for item in series.bars))

    def test_wrong_ratio_between_observations_is_a_conflict(self):
        pre = fixture_bars(self.PRE_SPLIT, scale=Decimal(10))
        post = fixture_bars(self.POST_SPLIT)
        wrong = SplitEvent(date(2024, 6, 10), Decimal("4"), ("YAHOO",))
        series = build_price_series(pre + post, split_events=(wrong,), as_of=self.POST_SPLIT)
        self.assertIn("PRICE_BASIS_CONFLICT", series.review_reasons)

    def test_observations_after_as_of_are_invisible(self):
        pre = fixture_bars(self.PRE_SPLIT, scale=Decimal(10))
        post = fixture_bars(self.POST_SPLIT)
        as_of = datetime(2024, 6, 3, tzinfo=UTC)
        series = build_price_series(pre + post, split_events=(NVDA_SPLIT,), as_of=as_of)
        self.assertTrue(all(item.observed_at == self.PRE_SPLIT for item in series.bars))
        self.assertTrue(all(item.bar_date <= as_of.date() for item in series.bars))

    def test_bars_after_as_of_are_invisible(self):
        post = fixture_bars(self.POST_SPLIT)
        as_of = datetime(2024, 6, 1, tzinfo=UTC)
        series = build_price_series(post, split_events=(NVDA_SPLIT,), as_of=as_of)
        self.assertEqual(series.bars, ())  # observed in 2026, after as_of

    def test_unrecorded_split_jump_requires_review(self):
        # One observation whose history was not adjusted for the split.
        unadjusted = fixture_bars(self.POST_SPLIT, scale=Decimal(10))
        post = fixture_bars(self.POST_SPLIT)
        stitched = [item for item in unadjusted if item.bar_date < date(2024, 6, 10)] + [
            item for item in post if item.bar_date >= date(2024, 6, 10)
        ]
        series = build_price_series(stitched, split_events=(), as_of=self.POST_SPLIT)
        self.assertIn("PRICE_SUSPECTED_UNRECORDED_SPLIT", series.review_reasons)
        self.assertIn("PRICE_SUSPECTED_UNRECORDED_SPLIT:2024-06-10", series.diagnostics)

    def test_a_large_real_move_without_gap_at_open_is_not_a_split(self):
        days = [date(2026, 9, 28) + timedelta(days=offset) for offset in range(3)]
        bars = [bar(days[0], 100), bar(days[1], 50, open_=98, high=99, low=50), bar(days[2], 51)]
        series = build_price_series(bars, split_events=(), as_of=datetime(2026, 10, 2, tzinfo=UTC))
        self.assertEqual(series.review_reasons, ())

    def test_observation_near_a_split_is_uncertain(self):
        observed = datetime(2024, 6, 12, 14, tzinfo=UTC)
        bars = [bar(date(2024, 6, 11), 120, observed_at=observed)]
        series = build_price_series(bars, split_events=(NVDA_SPLIT,), as_of=observed)
        self.assertIn("PRICE_BASIS_UNCERTAIN", series.review_reasons)

    def test_non_usd_currency_requires_review(self):
        bars = [bar(date(2026, 9, 30), 10, currency="EUR")]
        series = build_price_series(bars, split_events=(), as_of=datetime(2026, 10, 2, tzinfo=UTC))
        self.assertIn("PRICE_CURRENCY_UNSUPPORTED", series.review_reasons)


if __name__ == "__main__":
    unittest.main()
