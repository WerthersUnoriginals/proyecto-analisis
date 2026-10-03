"""Offline tests for the Experience Store outcomes and calibration (experience-outcomes-v1)."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.experience_outcomes_v1 import (
    HORIZONS,
    OUTCOMES_VERSION,
    calibration,
    composite_bucket,
    compute_outcome,
    entry_bar,
)
from database.prices_v3 import AdjustedBar
from test_new_highs_v1 import sessions

END = date(2026, 10, 2)


def bars(closes, highs=None, lows=None, end=END):
    days = sessions(len(closes), end=end)
    highs = highs or closes
    lows = lows or closes
    return [AdjustedBar(day, Decimal(str(c)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)), Decimal(1),
                        Decimal(1), None) for day, c, h, l in zip(days, closes, highs, lows)]


class EntryTests(unittest.TestCase):
    def test_version_and_horizons(self):
        self.assertEqual(OUTCOMES_VERSION, "experience-outcomes-v1")
        self.assertEqual(HORIZONS, (21, 63, 126, 252))

    def test_entry_is_the_last_session_on_or_before_as_of(self):
        series = bars([100] * 10)
        self.assertEqual(entry_bar(series, datetime(2026, 10, 3, 12, tzinfo=timezone.utc)).bar_date, date(2026, 10, 2))
        self.assertEqual(entry_bar(series, datetime(2026, 9, 30, tzinfo=timezone.utc)).bar_date, date(2026, 9, 30))
        self.assertIsNone(entry_bar(series, datetime(2020, 1, 1, tzinfo=timezone.utc)))


class OutcomeTests(unittest.TestCase):
    def test_returns_excess_drawdown_and_gain(self):
        closes = [100] + [100] * 20 + [110]                          # entry, 20 sessions, exit at +21
        lows = [100] + [95] * 20 + [110]
        highs = [100] + [104] * 20 + [111]
        stock = bars(closes, highs, lows)
        market = bars([200] * 21 + [210])
        result = compute_outcome(stock, market, stock[0].bar_date)
        h21 = result["horizons"][21]
        self.assertEqual(h21["status"], "MATURED")
        self.assertEqual(h21["return_pct"], 10.0)
        self.assertEqual(h21["spy_return_pct"], 5.0)
        self.assertEqual(h21["excess_pct"], 5.0)
        self.assertEqual(h21["max_drawdown_pct"], -5.0)
        self.assertEqual(h21["max_gain_pct"], 11.0)
        self.assertEqual(result["horizons"][63]["status"], "PENDING")

    def test_oneil_rule(self):
        stop = bars([100, 99, 95, 93, 130], lows=[100, 98, 94, 91, 129])          # -9% low before +20%
        self.assertEqual(compute_outcome(stop, stop, stop[0].bar_date)["oneil_rule"], "STOP_FIRST")
        target = bars([100, 105, 121, 90], highs=[100, 106, 122, 91], lows=[100, 104, 120, 89])
        self.assertEqual(compute_outcome(target, target, target[0].bar_date)["oneil_rule"], "TARGET_FIRST")
        same_day = bars([100, 100], highs=[100, 125], lows=[100, 90])
        self.assertEqual(compute_outcome(same_day, same_day, same_day[0].bar_date)["oneil_rule"], "STOP_FIRST")
        quiet = bars([100] * 300)
        self.assertEqual(compute_outcome(quiet, quiet, quiet[0].bar_date)["oneil_rule"], "NEITHER")
        young = bars([100] * 30)
        self.assertEqual(compute_outcome(young, young, young[0].bar_date)["oneil_rule"], "PENDING")

    def test_missing_market_bar_leaves_excess_unknown(self):
        stock = bars([100] * 21 + [110])
        market = bars([200] * 10)
        h21 = compute_outcome(stock, market, stock[0].bar_date)["horizons"][21]
        self.assertEqual(h21["return_pct"], 10.0)
        self.assertIsNone(h21["excess_pct"])

    def test_entry_not_in_series(self):
        self.assertEqual(compute_outcome(bars([1] * 5), bars([1] * 5), date(2000, 1, 3))["status"], "NO_ENTRY_PRICE")


class CalibrationTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual([composite_bucket(value) for value in (None, 30, 40, 55, 69.99, 70)],
                         [None, "<40", "40-55", "55-70", "55-70", ">=70"])

    def test_groups_only_matured_outcomes(self):
        def record(verdict, composite, ret, excess, rule="NEITHER", status="MATURED"):
            return {"verdict": verdict, "composite_score": composite, "outcome": {
                "status": "OK", "oneil_rule": rule,
                "horizons": {21: {"status": status, "return_pct": ret, "excess_pct": excess}}}}

        rows = [record("CANDIDATE", 80, 10.0, 4.0, "TARGET_FIRST"), record("CANDIDATE", 75, -2.0, -1.0),
                record("NOT_CANDIDATE", 30, -5.0, -6.0, "STOP_FIRST"), record("NOT_CANDIDATE", 35, 0, 0, status="PENDING")]
        table = calibration(rows)
        candidate = table["by_verdict"]["CANDIDATE"][21]
        self.assertEqual((candidate["count"], candidate["mean_return_pct"], candidate["mean_excess_pct"]), (2, 4.0, 1.5))
        self.assertEqual(candidate["positive_excess_share"], 0.5)
        self.assertEqual(candidate["target_first_share"], 0.5)
        weak = table["by_bucket"]["<40"][21]
        self.assertEqual((weak["count"], weak["stop_first_share"]), (1, 1.0))


def letter_results(score=80.0):
    from test_canslim_score_v1 import letter, market

    results = [letter(code, score) for code in "CANSLI"]
    results[2]["contract"].update({"last_bar_date": "2026-10-02", "close_last": 187.5})
    return tuple(results) + (market(),)


class SnapshotTests(unittest.TestCase):
    def test_record_holds_the_composite_and_the_full_payload(self):
        from database.experience_v1 import build_record

        record = build_record("NVDA", letter_results())
        self.assertEqual((record["verdict"], record["composite_score"], record["letters_passed"]), ("CANDIDATE", 80.0, 6))
        self.assertEqual((record["entry_bar_date"], record["entry_close"]), ("2026-10-02", 187.5))
        self.assertEqual(set(record["payload"]["letters"]), set("CANSLI"))
        self.assertEqual(record["market"]["contract"]["market_state"], "CONFIRMED_UPTREND")

    def test_snapshot_stores_one_transaction_and_reports_errors(self):
        from unittest import mock

        from database import evidence_v3
        from database.experience_v1 import take_snapshot

        cursor = mock.MagicMock()
        cursor.fetchone.return_value = (11,)
        connection = mock.MagicMock()
        connection.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
        companies = {"NVDA": (4, "0001045810")}
        with mock.patch.object(evidence_v3, "load_company", side_effect=lambda t, **kw: companies.get(t)):
            result = take_snapshot(["nvda", "ZZZZ", "NVDA"], label="test", clock=lambda: datetime(2026, 10, 4, tzinfo=timezone.utc),
                                   evaluate=lambda ticker, as_of: letter_results(), connection_factory=lambda: connection)
        self.assertEqual((result["snapshot_id"], result["records"]), (11, 1))
        self.assertEqual([item["ticker"] for item in result["errors"]], ["ZZZZ"])
        rows = cursor.executemany.call_args[0][1]
        self.assertEqual([(row[0], row[1], row[2], row[4]) for row in rows], [(11, 4, "NVDA", "CANDIDATE")])


if __name__ == "__main__":
    unittest.main()
