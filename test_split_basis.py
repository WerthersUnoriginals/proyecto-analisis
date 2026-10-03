"""Offline tests for split-basis reconciliation and conversion (split-basis-v1)."""

import unittest
from datetime import date
from decimal import Decimal

from database.sec_facts import SecFact
from database.split_basis import (
    SPLIT_BASIS_VERSION,
    SplitEvent,
    basis_factor,
    basis_uncertain,
    reconcile_split_events,
    suspected_split_ratio,
)


def ratio_fact(end, value, start=None, filed=date(2024, 8, 28), accession="a1"):
    return SecFact(
        taxonomy="us-gaap", tag="StockholdersEquityNoteStockSplitConversionRatio1",
        unit="pure", period_start=start, period_end=end, value=Decimal(value),
        accession=accession, fiscal_year=2025, fiscal_period="Q2", form="10-Q",
        filed_date=filed, frame=None,
    )


AS_OF = date(2026, 10, 1)
WINDOW = date(2020, 10, 1)


class ReconcileTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(SPLIT_BASIS_VERSION, "split-basis-v1")

    def test_no_capture_is_unknown(self):
        result = reconcile_split_events(None, [], window_start=WINDOW, as_of=AS_OF)
        self.assertEqual(result.status, "UNKNOWN")

    def test_no_events_anywhere_is_no_recent_splits(self):
        result = reconcile_split_events([], [], window_start=WINDOW, as_of=AS_OF)
        self.assertEqual(result.status, "NO_RECENT_SPLITS")
        self.assertEqual(result.events, ())

    def test_provider_event_corroborated_by_sec_duration_fact(self):
        result = reconcile_split_events(
            [(date(2024, 6, 10), Decimal("10"))],
            [ratio_fact(date(2024, 5, 31), "10", start=date(2024, 5, 1))],
            window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.status, "VERIFIED_ALREADY_ADJUSTED")
        self.assertEqual(result.events, (SplitEvent(date(2024, 6, 10), Decimal("10"), ("SEC", "YAHOO")),))

    def test_provider_event_corroborated_by_sec_instant_fact(self):
        result = reconcile_split_events(
            [(date(2021, 7, 20), Decimal("4"))],
            [ratio_fact(date(2021, 7, 19), "4")],
            window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.status, "VERIFIED_ALREADY_ADJUSTED")

    def test_sec_split_missing_from_provider_fails_closed(self):
        result = reconcile_split_events(
            [], [ratio_fact(date(2024, 5, 31), "10", start=date(2024, 5, 1))],
            window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertIn("SEC_SPLIT_NOT_IN_PROVIDER", result.reasons)
        # The SEC-only event is still exposed for diagnostics and adjustment.
        self.assertEqual(result.events[0].sources, ("SEC",))

    def test_ratio_disagreement_fails_closed(self):
        result = reconcile_split_events(
            [(date(2024, 6, 10), Decimal("4"))],
            [ratio_fact(date(2024, 5, 31), "10", start=date(2024, 5, 1))],
            window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.status, "REVIEW_REQUIRED")

    def test_provider_only_event_is_uncorroborated_until_pairs_verify_it(self):
        result = reconcile_split_events(
            [(date(2024, 6, 10), Decimal("10"))], [], window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.events[0].sources, ("YAHOO",))
        self.assertIn("PROVIDER_SPLIT_NOT_IN_SEC", result.reasons)

    def test_old_sec_facts_outside_window_are_ignored(self):
        result = reconcile_split_events(
            [], [ratio_fact(date(2014, 6, 6), "7")], window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.status, "NO_RECENT_SPLITS")

    def test_several_sec_dates_for_one_split_describe_one_event(self):
        # NVDA reports its 2021 4:1 split at 2021-06-03 (approval) and 2021-07-19
        # (effective); yfinance has 2021-07-20. One event, not two.
        from test_quarterly_v3 import real_facts

        result = reconcile_split_events(
            [(date(2021, 7, 20), Decimal("4.0")), (date(2024, 6, 10), Decimal("10.0"))],
            real_facts("nvda"), window_start=date(2020, 10, 2), as_of=date(2026, 10, 2),
        )
        self.assertEqual(result.status, "VERIFIED_ALREADY_ADJUSTED")
        self.assertEqual([event.event_date for event in result.events], [date(2021, 7, 20), date(2024, 6, 10)])
        self.assertEqual(result.reasons, ())

    def test_sec_only_dates_of_one_split_are_one_event(self):
        result = reconcile_split_events(
            [], [ratio_fact(date(2021, 6, 3), "4"), ratio_fact(date(2021, 7, 19), "4", accession="a2")],
            window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(len(result.events), 1)

    def test_sec_split_before_first_periodic_filing_is_not_a_review(self):
        # CRWV split before its IPO: every 10-Q/10-K was filed after it, so all
        # published values already share the post-split basis.
        periodic = SecFact(
            taxonomy="us-gaap", tag="NetIncomeLoss", unit="USD", period_start=date(2025, 1, 1),
            period_end=date(2025, 3, 31), value=Decimal(1), accession="q1", fiscal_year=2025,
            fiscal_period="Q1", form="10-Q", filed_date=date(2025, 5, 15), frame=None,
        )
        result = reconcile_split_events(
            [], [ratio_fact(date(2025, 2, 20), "2"), periodic], window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.status, "NO_RECENT_SPLITS")
        self.assertIn("SEC_SPLIT_BEFORE_FIRST_PERIODIC_FILING", result.reasons)

    def test_events_after_as_of_are_ignored(self):
        result = reconcile_split_events(
            [(date(2027, 1, 1), Decimal("2"))], [], window_start=WINDOW, as_of=AS_OF,
        )
        self.assertEqual(result.status, "NO_RECENT_SPLITS")


class FactorTests(unittest.TestCase):
    events = (
        SplitEvent(date(2021, 7, 20), Decimal("4"), ("SEC", "YAHOO")),
        SplitEvent(date(2024, 6, 10), Decimal("10"), ("SEC", "YAHOO")),
    )

    def test_factor_multiplies_events_after_basis_date(self):
        self.assertEqual(basis_factor(self.events, date(2021, 5, 26), AS_OF), Decimal("40"))
        self.assertEqual(basis_factor(self.events, date(2023, 8, 28), AS_OF), Decimal("10"))
        self.assertEqual(basis_factor(self.events, date(2024, 8, 28), AS_OF), Decimal("1"))

    def test_factor_ignores_events_after_as_of(self):
        self.assertEqual(basis_factor(self.events, date(2021, 5, 26), date(2023, 1, 1)), Decimal("4"))

    def test_basis_uncertain_within_seven_days(self):
        self.assertTrue(basis_uncertain(self.events, date(2024, 6, 5)))
        self.assertTrue(basis_uncertain(self.events, date(2024, 6, 17)))
        self.assertFalse(basis_uncertain(self.events, date(2024, 5, 29)))


class SuspectedRatioTests(unittest.TestCase):
    def test_detects_common_ratios_and_inverses(self):
        self.assertEqual(suspected_split_ratio(Decimal("2.48"), Decimal("0.25")), Decimal("10"))
        self.assertEqual(suspected_split_ratio(Decimal("0.25"), Decimal("2.48")), Decimal("0.1"))
        self.assertEqual(suspected_split_ratio(Decimal("3.03"), Decimal("0.76")), Decimal("4"))

    def test_large_ratios(self):
        self.assertEqual(suspected_split_ratio(Decimal("65.0"), Decimal("1.30")), Decimal("50"))  # CMG 2024
        self.assertEqual(suspected_split_ratio(Decimal("7.0"), Decimal("1.0")), Decimal("7"))   # AAPL 2014

    def test_ordinary_restatement_is_not_a_split(self):
        self.assertIsNone(suspected_split_ratio(Decimal("1.05"), Decimal("1.00")))
        self.assertIsNone(suspected_split_ratio(Decimal("0"), Decimal("1")))


if __name__ == "__main__":
    unittest.main()
