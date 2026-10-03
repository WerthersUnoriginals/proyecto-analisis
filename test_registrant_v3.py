"""Offline tests for registrant profiling (foreign filers, reorganizations)."""

import unittest
from datetime import date, datetime, timezone

from database.registrant_v3 import (
    REGISTRANT_VERSION,
    RegistrantLink,
    choose_predecessors,
    classify_filer,
    parse_filers_header,
    registrant_history,
    succession_filings,
)

UTC = timezone.utc
AS_OF = datetime(2026, 10, 2, tzinfo=UTC)


class FilerTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(REGISTRANT_VERSION, "registrant-v1")

    def test_domestic_filer(self):
        filings = [("10-K", date(2025, 10, 31)), ("10-Q", date(2026, 7, 31))]
        self.assertEqual(classify_filer(filings, AS_OF), "DOMESTIC")

    def test_foreign_filers(self):
        for annual in ("20-F", "40-F"):
            with self.subTest(annual=annual):
                filings = [(annual, date(2026, 4, 16)), ("6-K", date(2026, 9, 24))]
                self.assertEqual(classify_filer(filings, AS_OF), "FOREIGN_FILER_NOT_SUPPORTED")

    def test_switch_from_20f_to_10k_is_domestic(self):
        filings = [("20-F", date(2023, 4, 1)), ("10-K", date(2026, 2, 1))]
        self.assertEqual(classify_filer(filings, AS_OF), "DOMESTIC")

    def test_old_20f_outside_window_is_ignored(self):
        filings = [("20-F", date(2019, 4, 1)), ("10-Q", date(2026, 8, 1))]
        self.assertEqual(classify_filer(filings, AS_OF), "DOMESTIC")

    def test_no_periodic_filings(self):
        self.assertEqual(classify_filer([("8-K", date(2026, 1, 1))], AS_OF), "NO_PERIODIC_FILINGS")

    def test_future_filings_are_invisible(self):
        filings = [("20-F", date(2027, 1, 1)), ("10-K", date(2026, 2, 1))]
        self.assertEqual(classify_filer(filings, AS_OF), "DOMESTIC")


HEADER = """
FILER:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			EXXON MOBIL CORP
		CENTRAL INDEX KEY:			0000034088
FILER:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			ExxonMobil Holdings Corp
		CENTRAL INDEX KEY:			0002115436
"""


class SuccessionTests(unittest.TestCase):
    def test_parse_filers_header(self):
        self.assertEqual(parse_filers_header(HEADER), [
            ("0000034088", "EXXON MOBIL CORP"), ("0002115436", "ExxonMobil Holdings Corp"),
        ])

    def test_succession_filings(self):
        filings = [("8-K12B", date(2026, 7, 1), "acc1"), ("10-Q", date(2026, 8, 3), "acc2"),
                   ("8-K12G3", date(2020, 1, 1), "acc0")]
        self.assertEqual(succession_filings(filings), [("8-K12G3", date(2020, 1, 1), "acc0"),
                                                       ("8-K12B", date(2026, 7, 1), "acc1")])

    def test_predecessor_must_cofile_and_have_history(self):
        joint = {"acc2": ["0000034088", "0002115436"], "acc3": ["0002115436", "0000099999"]}
        history = {
            "0000034088": [("10-K", date(2026, 2, 18)), ("10-Q", date(2026, 5, 1))],
            "0000099999": [("8-K", date(2026, 1, 1))],  # co-filer without periodic history
        }
        self.assertEqual(
            choose_predecessors("0002115436", date(2026, 7, 1), joint, history), ["0000034088"],
        )

    def test_history_is_reported_and_unresolved_goes_to_review(self):
        linked = RegistrantLink("0002115436", "0000034088", "8-K12B", "acc1", date(2026, 7, 1), "LINKED")
        missing = RegistrantLink("0002115436", None, "8-K12B", "acc1", date(2026, 7, 1), "PREDECESSOR_NOT_FOUND")
        entries, diagnostics, review = registrant_history([linked])
        self.assertEqual(entries[0]["predecessor_cik"], "0000034088")
        self.assertIn("REGISTRANT_SUCCESSION:2026-07-01:0000034088->0002115436", diagnostics)
        self.assertFalse(review)
        _, diagnostics, review = registrant_history([missing])
        self.assertIn("REGISTRANT_SUCCESSION_UNRESOLVED", diagnostics)
        self.assertTrue(review)

    def test_unresolved_old_succession_does_not_force_review(self):
        # AVGO 2018: Singapore -> Delaware reorganization, outside the evidence window.
        old = RegistrantLink("0001730168", None, "8-K12B", "acc", date(2018, 4, 4), "PREDECESSOR_NOT_FOUND")
        entries, diagnostics, review = registrant_history([old], AS_OF)
        self.assertFalse(review)
        self.assertFalse(entries[0]["affects_analysis_window"])
        self.assertIn("REGISTRANT_SUCCESSION_OUTSIDE_WINDOW", diagnostics)


if __name__ == "__main__":
    unittest.main()
