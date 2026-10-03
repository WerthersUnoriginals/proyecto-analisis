"""Offline tests for 13F parsing, CUSIP resolution and I calculations (i-institutional-v1)."""

import io
import unittest
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from database.institutional_v1 import (
    CUSIP_RESOLUTION_VERSION,
    dataset_window,
    normalize_issuer,
    parse_13f_dataset,
    resolve_cusip,
)
from database.sponsorship_v1 import (
    SPONSORSHIP_VERSION,
    Filing13F,
    Holding13F,
    complete_quarters,
    compute_sponsorship,
    effective_accessions,
)

UTC = timezone.utc


class NameTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_issuer("Rivian Automotive, Inc. / DE"), "RIVIAN AUTOMOTIVE")
        self.assertEqual(normalize_issuer("RIVIAN AUTOMOTIVE INC COM CL A"), "RIVIAN AUTOMOTIVE")
        self.assertEqual(normalize_issuer("US BANCORP \\DE\\"), "US BANCORP")
        self.assertEqual(normalize_issuer("HORTON D R INC /DE/"), "HORTON D R")
        self.assertEqual(normalize_issuer("The Walt Disney Company"), "WALT DISNEY")

    def test_name_match_needs_a_dominant_cusip(self):
        counts = {"REDDIT": Counter({"75734B100": 2350, "75734B999": 3})}
        self.assertEqual(resolve_cusip("Reddit, Inc.", None, counts)[:2], ("75734B100", "CUSIP_FROM_NAME_MATCH"))
        ambiguous = {"ALPHABET": Counter({"02079K305": 6000, "02079K107": 5500})}
        self.assertEqual(resolve_cusip("Alphabet Inc.", None, ambiguous)[:2], (None, "CUSIP_AMBIGUOUS"))
        few = {"TINY": Counter({"000000001": 10})}
        self.assertEqual(resolve_cusip("Tiny Inc", None, few)[:2], (None, "CUSIP_AMBIGUOUS"))
        self.assertEqual(resolve_cusip("Nobody Corp", None, {})[:2], (None, "CUSIP_NOT_FOUND"))

    def test_official_cusip_wins_and_disagreement_is_reported(self):
        counts = {"ONEOK": Counter({"682680103": 3000})}
        self.assertEqual(resolve_cusip("ONEOK INC /NEW/", "30609A109", counts)[:2],
                         ("30609A109", "CUSIP_SOURCES_DISAGREE"))
        agree = {"NVIDIA": Counter({"67066G104": 7000})}
        self.assertEqual(resolve_cusip("NVIDIA CORP", "67066G104", agree)[:2], ("67066G104", "CUSIP_OFFICIAL"))
        self.assertEqual(resolve_cusip("GOOGLE", "02079K305", {})[:2], ("02079K305", "CUSIP_OFFICIAL"))

    def test_versions(self):
        self.assertEqual(CUSIP_RESOLUTION_VERSION, "cusip-resolution-v1")
        self.assertEqual(SPONSORSHIP_VERSION, "i-institutional-v1")


def dataset(submissions, covers, infotable, folder=""):
    def tsv(header, rows):
        return "\t".join(header) + "\n" + "".join("\t".join(row) + "\n" for row in rows)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(folder + "SUBMISSION.tsv", tsv(["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK",
                                                 "PERIODOFREPORT"], submissions))
        archive.writestr(folder + "COVERPAGE.tsv", tsv(["ACCESSION_NUMBER", "REPORTCALENDARORQUARTER", "ISAMENDMENT",
                                                "AMENDMENTNO", "AMENDMENTTYPE"], covers))
        archive.writestr(folder + "INFOTABLE.tsv", tsv(["ACCESSION_NUMBER", "INFOTABLE_SK", "NAMEOFISSUER", "TITLEOFCLASS",
                                                "CUSIP", "FIGI", "VALUE", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"],
                                               infotable))
    return buffer.getvalue()


class DatasetTests(unittest.TestCase):
    def test_tables_inside_a_folder_are_read(self):
        payload = dataset([["A1", "14-AUG-2025", "13F-HR", "1", "30-JUN-2025"]], [["A1", "30-JUN-2025", "N", "", ""]],
                          [["A1", "1", "NVIDIA CORP", "COM", "67066G104", "", "1", "1", "SH", ""]],
                          folder="01JUN2025-31AUG2025_form13f/")
        filings, holdings, _ = parse_13f_dataset(io.BytesIO(payload), tracked={"67066G104"}, wanted_names=set())
        self.assertEqual((len(filings), len(holdings)), (1, 1))

    def test_window_names(self):
        self.assertEqual(dataset_window("01jun2026-31aug2026_form13f.zip"), (date(2026, 6, 1), date(2026, 8, 31)))
        self.assertIsNone(dataset_window("2023q4_form13f.zip"))

    def test_parse_keeps_filings_tracked_rows_and_name_counts(self):
        payload = dataset(
            [["A1", "14-AUG-2026", "13F-HR", "0000000001", "30-JUN-2026"],
             ["A2", "15-AUG-2026", "13F-HR/A", "0000000001", "30-JUN-2026"],
             ["N1", "14-AUG-2026", "13F-NT", "0000000003", "30-JUN-2026"]],
            [["A1", "30-JUN-2026", "N", "", ""], ["A2", "30-JUN-2026", "Y", "1", "RESTATEMENT"]],
            [["A1", "1", "NVIDIA CORP", "COM", "67066G104", "", "100", "10", "SH", ""],
             ["A1", "2", "REDDIT INC CL A", "CL A", "75734B100", "", "5", "1", "SH", ""],
             ["A1", "3", "NVIDIA CORP", "CALL", "67066G104", "", "100", "10", "SH", "Call"],
             ["A2", "1", "OTHER CO", "COM", "000000000", "", "1", "1", "SH", ""]],
        )
        filings, holdings, names = parse_13f_dataset(io.BytesIO(payload), tracked={"67066G104"}, wanted_names={"REDDIT"})
        self.assertEqual([(item.accession, item.submission_type, item.amendment_type) for item in filings],
                         [("A1", "13F-HR", None), ("A2", "13F-HR/A", "RESTATEMENT")])
        self.assertEqual(filings[0].filing_date, date(2026, 8, 14))
        self.assertEqual(filings[0].period_of_report, date(2026, 6, 30))
        self.assertEqual([(item.accession, item.infotable_sk, item.put_call) for item in holdings],
                         [("A1", "1", None), ("A1", "3", "Call")])
        self.assertEqual(holdings[0].shares, Decimal(10))
        self.assertEqual(names, {"REDDIT": Counter({"75734B100": 1})})


Q2, Q1, Q4, Q3, Q2_PREV = (date(2026, 6, 30), date(2026, 3, 31), date(2025, 12, 31), date(2025, 9, 30),
                           date(2025, 6, 30))


def filing(accession, cik, filed, period, kind="13F-HR", amendment=None):
    return Filing13F(accession, cik, filed, kind, period, amendment)


def holding(accession, shares, cusip="C", put_call=None, shares_type="SH"):
    return Holding13F(accession, "1", cusip, "X", "COM", Decimal(0), Decimal(shares), shares_type, put_call)


class QuarterTests(unittest.TestCase):
    def test_complete_quarters_respect_the_45_day_deadline(self):
        self.assertEqual(complete_quarters(date(2026, 10, 3), 2), [Q2, Q1])
        self.assertEqual(complete_quarters(date(2026, 8, 13), 2), [Q1, Q4])
        self.assertEqual(complete_quarters(date(2026, 8, 14), 1), [Q2])


class EffectiveTests(unittest.TestCase):
    def test_restatement_replaces_and_new_holdings_add(self):
        filings = [
            filing("O", "1", date(2026, 8, 1), Q2),
            filing("R", "1", date(2026, 8, 10), Q2, "13F-HR/A", "RESTATEMENT"),
            filing("N", "1", date(2026, 8, 20), Q2, "13F-HR/A", "NEW HOLDINGS"),
            filing("OLD_N", "1", date(2026, 8, 5), Q2, "13F-HR/A", "NEW HOLDINGS"),  # before the restatement
            filing("X", "2", date(2026, 8, 1), Q2),
        ]
        self.assertEqual(effective_accessions(filings, Q2, date(2026, 10, 3)), {"R", "N", "X"})
        self.assertEqual(effective_accessions(filings, Q2, date(2026, 8, 9)), {"O", "OLD_N", "X"})


class SponsorshipTests(unittest.TestCase):
    def history(self):
        """Holders per quarter: Q2_PREV 2, Q3 2, Q4 3, Q1 4, Q2 5."""
        filings, holdings = [], []
        counts = {Q2_PREV: 2, Q3: 2, Q4: 3, Q1: 4, Q2: 5}
        for period, count in counts.items():
            filed = period + timedelta(days=40)
            for cik in range(count):
                accession = f"{period}-{cik}"
                filings.append(filing(accession, str(cik), filed, period))
                holdings.append(holding(accession, 100))
        return filings, holdings

    def test_counts_and_changes(self):
        filings, holdings = self.history()
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=Decimal(1000))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.latest_quarter, Q2)
        self.assertEqual(result.holders_by_quarter, {Q2: 5, Q1: 4, Q4: 3, Q3: 2, Q2_PREV: 2})
        self.assertEqual(result.holders_change_qoq_pct, Decimal(25))
        self.assertEqual(result.holders_change_yoy_pct, Decimal(150))
        self.assertEqual(result.quarters_increasing, 3)
        self.assertEqual(result.institutional_shares, Decimal(500))
        self.assertEqual(result.ownership_pct, Decimal(50))

    def test_options_and_principal_amounts_do_not_count(self):
        filings = [filing("A", "1", date(2026, 8, 1), Q2), filing("B", "2", date(2026, 8, 1), Q2)]
        holdings = [holding("A", 10, put_call="Put"), holding("B", 10, shares_type="PRN")]
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=None, quarters=1)
        self.assertEqual(result.holders_by_quarter, {Q2: 0})

    def test_a_restatement_without_the_cusip_removes_the_holder(self):
        filings = [filing("O", "1", date(2026, 8, 1), Q2),
                   filing("R", "1", date(2026, 8, 10), Q2, "13F-HR/A", "RESTATEMENT")]
        result = compute_sponsorship(filings, [holding("O", 10)], "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=None, quarters=1)
        self.assertEqual(result.holders_by_quarter, {Q2: 0})

    def test_missing_quarters_are_insufficient(self):
        filings, holdings = self.history()
        filings = [item for item in filings if item.period_of_report != Q2_PREV]
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=Decimal(1000))
        self.assertEqual(result.status, "INSUFFICIENT_HISTORY")
        self.assertIsNone(result.holders_change_yoy_pct)
        self.assertEqual(result.holders_change_qoq_pct, Decimal(25))

    def test_split_after_quarter_end_converts_shares(self):
        from database.split_basis import SplitEvent

        filings, holdings = self.history()
        event = SplitEvent(date(2026, 9, 1), Decimal(2), ("SEC", "YAHOO"))
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=Decimal(2000), split_events=(event,))
        self.assertEqual(result.institutional_shares, Decimal(1000))
        self.assertEqual(result.ownership_pct, Decimal(50))

    def test_ownership_above_100_is_flagged(self):
        filings, holdings = self.history()
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=Decimal(400))
        self.assertIn("OWNERSHIP_ABOVE_100_PCT", result.diagnostics)

    def test_holder_count_discontinuity_requires_review(self):
        # XOM, July 2026: the new CUSIP had 4 holders at Q1 and 398 at Q2.
        filings, holdings = [], []
        for period, count in {Q2_PREV: 4, Q3: 4, Q4: 4, Q1: 4, Q2: 398}.items():
            for cik in range(count):
                accession = f"{period}-{cik}"
                filings.append(filing(accession, str(cik), period + timedelta(days=40), period))
                holdings.append(holding(accession, 1))
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=None)
        self.assertEqual(result.review_reasons, ("HOLDERS_DISCONTINUITY",))
        self.assertIn("HOLDERS_DISCONTINUITY:2026-06-30", result.diagnostics)

    def test_normal_growth_is_not_a_discontinuity(self):
        filings, holdings = self.history()
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 10, 3, tzinfo=UTC),
                                     shares_outstanding=Decimal(1000))
        self.assertEqual(result.review_reasons, ())

    def test_filings_after_as_of_are_invisible(self):
        filings, holdings = self.history()
        result = compute_sponsorship(filings, holdings, "C", as_of=datetime(2026, 7, 1, tzinfo=UTC),
                                     shares_outstanding=Decimal(1000), quarters=1)
        self.assertEqual(result.latest_quarter, Q1)


if __name__ == "__main__":
    unittest.main()
