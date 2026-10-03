"""Offline tests for informative SEC catalysts (sec-catalysts-v1)."""

import unittest
from datetime import date, datetime, timezone

from database.catalysts_v3 import CATALYSTS_VERSION, CatalystFiling, select_catalysts
from database.providers_v3 import parse_submission_arrays

UTC = timezone.utc
AS_OF = datetime(2026, 10, 3, 12, tzinfo=UTC)


def filing(accession, filed, items, *, form="8-K", acceptance=None, cik="0001045810"):
    return CatalystFiling(
        cik=cik, accession=accession, form=form, filing_date=filed,
        acceptance_at=acceptance if acceptance is not None else datetime.combine(filed, datetime.min.time(), UTC).replace(hour=21),
        items=tuple(items),
    )


class ItemsParsingTests(unittest.TestCase):
    def test_items_are_parsed_literally(self):
        records = parse_submission_arrays({
            "accessionNumber": ["a", "b"], "filingDate": ["2026-09-01", "2026-09-02"],
            "reportDate": ["", ""], "acceptanceDateTime": ["2026-09-01T21:00:00.000Z", "2026-09-02T21:00:00.000Z"],
            "form": ["8-K", "10-Q"], "items": ["5.02,9.01", ""],
        })
        self.assertEqual(records[0].items, ("5.02", "9.01"))
        self.assertEqual(records[1].items, ())

    def test_missing_items_column_means_no_items(self):
        records = parse_submission_arrays({
            "accessionNumber": ["a"], "filingDate": ["2026-09-01"], "reportDate": [""],
            "acceptanceDateTime": ["2026-09-01T21:00:00.000Z"], "form": ["8-K"],
        })
        self.assertEqual(records[0].items, ())


class SelectionTests(unittest.TestCase):
    def test_management_and_acquisition_items_are_selected(self):
        result = select_catalysts([
            filing("0001045810-26-000001", date(2026, 9, 1), ["5.02", "9.01"]),
            filing("0001045810-26-000002", date(2026, 8, 1), ["2.01"]),
            filing("0001045810-26-000003", date(2026, 7, 1), ["7.01"]),
        ], AS_OF)
        self.assertEqual(result["version"], CATALYSTS_VERSION)
        self.assertEqual([event["item"] for event in result["events"]], ["5.02", "2.01"])
        self.assertEqual(result["counts"], {"5.02": 1, "2.01": 1})
        self.assertEqual(result["events"][0]["category"], "MANAGEMENT_CHANGE")
        self.assertEqual(
            result["events"][0]["url"],
            "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000001/",
        )

    def test_window_is_365_days(self):
        result = select_catalysts([filing("old", date(2025, 10, 2), ["5.02"]),
                                   filing("edge", date(2025, 10, 3), ["5.02"])], AS_OF)
        self.assertEqual([event["accession"] for event in result["events"]], ["edge"])

    def test_not_yet_accepted_filings_are_invisible(self):
        later = datetime(2026, 10, 3, 21, tzinfo=UTC)
        result = select_catalysts([filing("today", date(2026, 10, 3), ["5.02"], acceptance=later)], AS_OF)
        self.assertEqual(result["events"], [])

    def test_missing_acceptance_uses_end_of_filing_day_and_is_flagged(self):
        item = CatalystFiling(cik="0001045810", accession="x", form="8-K/A", filing_date=date(2026, 10, 3),
                              acceptance_at=None, items=("2.01",))
        self.assertEqual(select_catalysts([item], AS_OF)["events"], [])
        result = select_catalysts([item], datetime(2026, 10, 4, 6, tzinfo=UTC))
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("ACCEPTANCE_MISSING:x", result["diagnostics"])

    def test_other_forms_are_ignored(self):
        result = select_catalysts([filing("k", date(2026, 9, 1), ["5.02"], form="10-K")], AS_OF)
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
