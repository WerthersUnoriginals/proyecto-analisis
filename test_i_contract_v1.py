"""Offline tests for the I input contract (i-input-contract-v1), I Score v1 and the 13F ingestion."""

import io
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from database.i_contract_v1 import CONTRACT_VERSION, I_INPUT_KEYS, build_i_contract_v1
from database.split_basis import SplitReconciliation
from database.sponsorship_v1 import Sponsorship
from i_score_v1 import MODEL_VERSION, build_i_score

UTC = timezone.utc
AS_OF = datetime(2026, 10, 3, tzinfo=UTC)
NO_SPLITS = SplitReconciliation("NO_RECENT_SPLITS", ())
Q = [date(2026, 6, 30), date(2026, 3, 31), date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30)]


def sponsorship(**overrides):
    values = dict(status="OK", latest_quarter=Q[0], holders_by_quarter=dict(zip(Q, [520, 500, 480, 470, 450])),
                  holders_latest=520, holders_change_qoq_pct=Decimal(4), holders_change_yoy_pct=Decimal("15.5555"),
                  quarters_increasing=4, institutional_shares=Decimal(700), ownership_pct=Decimal(70))
    values.update(overrides)
    return Sponsorship(**values)


def contract(result=None, *, cusip="67066G104", source="CUSIP_OFFICIAL", filer_status="DOMESTIC",
             split_status="NO_RECENT_SPLITS"):
    return build_i_contract_v1(
        result or sponsorship(), NO_SPLITS, cusip=cusip, cusip_source=source, split_status=split_status,
        split_reasons=[], rejected_splits=(), company_id=1, as_of=AS_OF, filer_status=filer_status,
    )


class ContractTests(unittest.TestCase):
    def test_all_inputs_and_version(self):
        result = contract()
        self.assertEqual(CONTRACT_VERSION, "i-input-contract-v1")
        for key in I_INPUT_KEYS:
            self.assertIn(key, result)
            self.assertIn(key, result["i_input_contract"]["inputs"])
        self.assertEqual(result["holders_latest"], 520)
        self.assertEqual(result["holders_by_quarter"]["2026-06-30"], 520)
        self.assertEqual(result["i_data_integrity"], "VERIFIED")

    def test_name_matched_cusip_is_flagged_but_usable(self):
        result = contract(source="CUSIP_FROM_NAME_MATCH")
        self.assertEqual(result["i_data_integrity"], "VERIFIED")
        self.assertIn("CUSIP_FROM_NAME_MATCH", result["integrity"]["diagnostics"])

    def test_cusip_problems_require_review(self):
        for source in ("CUSIP_SOURCES_DISAGREE", "CUSIP_AMBIGUOUS", "CUSIP_NOT_FOUND"):
            with self.subTest(source=source):
                cusip = None if source != "CUSIP_SOURCES_DISAGREE" else "30609A109"
                result = contract(Sponsorship("INSUFFICIENT_HISTORY") if cusip is None else None,
                                  cusip=cusip, source=source)
                self.assertEqual(result["i_data_integrity"], "REVIEW_REQUIRED")

    def test_missing_quarters_require_review(self):
        result = contract(sponsorship(status="INSUFFICIENT_HISTORY", holders_change_yoy_pct=None,
                                      diagnostics=("MISSING_13F_QUARTERS",)))
        self.assertEqual(result["i_data_integrity"], "REVIEW_REQUIRED")

    def test_discontinuity_requires_review(self):
        result = contract(sponsorship(review_reasons=("HOLDERS_DISCONTINUITY",)))
        self.assertEqual(result["i_data_integrity"], "REVIEW_REQUIRED")

    def test_missing_share_count_is_partial(self):
        result = contract(sponsorship(ownership_pct=None))
        self.assertEqual(result["i_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")

    def test_foreign_filer_requires_review(self):
        self.assertEqual(contract(filer_status="FOREIGN_FILER_NOT_SUPPORTED")["i_data_integrity"], "REVIEW_REQUIRED")


class ScoreTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(MODEL_VERSION, "i-1.0-exp")

    def test_components(self):
        points = {name: item["points"] for name, item in build_i_score(contract())["i_score_v1"]["components"].items()}
        self.assertAlmostEqual(points["holders_qoq"], 20 + (2 / 3) * 6)
        self.assertAlmostEqual(points["holders_yoy"], 22 + (5.5555 / 10) * 3, places=4)
        self.assertEqual(points["quarters_increasing"], 15.0)
        self.assertEqual(points["ownership"], 30.0)

    def test_over_ownership_is_penalized(self):
        points = build_i_score(contract(sponsorship(ownership_pct=Decimal(110))))["i_score_v1"]["components"]
        self.assertEqual(points["ownership"]["points"], 12.0)

    def test_classic(self):
        self.assertEqual(build_i_score(contract())["i_classic"]["result"], "PASS")
        noise = contract(sponsorship(holders_change_qoq_pct=Decimal(-1)))
        self.assertEqual(build_i_score(noise)["i_classic"]["result"], "PASS")  # 1% tolerance
        declining = contract(sponsorship(holders_change_qoq_pct=Decimal("-1.01")))
        self.assertEqual(build_i_score(declining)["i_classic"]["result"], "FAIL_DECLINING_SPONSORSHIP")
        missing = contract(sponsorship(holders_change_yoy_pct=None))
        self.assertEqual(build_i_score(missing)["i_classic"]["result"], "INSUFFICIENT_DATA")


class IngestionTests(unittest.TestCase):
    def test_cusips_are_resolved_then_every_dataset_is_stored(self):
        from database import evidence_v3, institutional_v1
        from test_sponsorship_v1 import dataset

        payload = dataset(
            [["A1", "14-AUG-2026", "13F-HR", "1", "30-JUN-2026"]],
            [["A1", "30-JUN-2026", "N", "", ""]],
            [["A1", str(sk), "REDDIT INC", "CL A", "75734B100", "", "5", "1", "SH", ""] for sk in range(60)]
            + [["A1", "99", "NVIDIA CORP", "COM", "67066G104", "", "1", "1", "SH", ""]],
        )
        page = ('<a href="/files/x/01jun2026-31aug2026_form13f.zip">a</a>'
                '<a href="/files/x/01mar2026-31may2026_form13f.zip">b</a>'
                '<a href="/files/x/01mar2025-31may2025_form13f.zip">old</a>')
        cursor = mock.MagicMock()
        cursor.execute.return_value.fetchall.side_effect = [
            [(1, "RDDT", "Reddit, Inc."), (2, "NVDA", "NVIDIA CORP")],
            [("NVDA", "67066G104")],
        ]
        connection = mock.MagicMock()
        connection.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
        stored = []
        with mock.patch.object(evidence_v3, "insert_company_cusip") as cusips, \
                mock.patch.object(evidence_v3, "dataset_13f_stored", return_value=False), \
                mock.patch.object(evidence_v3, "insert_13f_dataset", return_value=7), \
                mock.patch.object(evidence_v3, "insert_13f_filings", return_value=1), \
                mock.patch.object(evidence_v3, "insert_13f_holdings",
                                  side_effect=lambda cursor, dataset_id, holdings: stored.append(holdings) or len(holdings)):
            summary = institutional_v1.ingest_institutional(
                quarters=2, clock=lambda: AS_OF, connection_factory=lambda: connection,
                page_getter=lambda url: page, downloader=lambda url: (io.BytesIO(payload), len(payload), "0" * 64),
            )
        self.assertEqual(summary["cusips"]["RDDT"], {"cusip": "75734B100", "source": "CUSIP_FROM_NAME_MATCH"})
        self.assertEqual(summary["cusips"]["NVDA"], {"cusip": "67066G104", "source": "CUSIP_OFFICIAL"})
        self.assertEqual([item["file"] for item in summary["datasets"]],
                         ["01mar2026-31may2026_form13f.zip", "01jun2026-31aug2026_form13f.zip"])
        self.assertEqual(len(stored[0]), 61)  # tracked RDDT and NVDA rows
        self.assertEqual(cusips.call_count, 2)


if __name__ == "__main__":
    unittest.main()
