"""Offline tests for the v3 C input contract built from the quarterly view."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from c_score_v1 import build_c_score
from database.c_contract_v3 import CONTRACT_VERSION, build_c_contract_v3
from database.c_dual_run_contract import INDEPENDENT_C_INPUT_KEYS
from database.quarterly_v3 import build_quarterly_view
from database.split_basis import SplitEvent, SplitReconciliation
from test_quarterly_v3 import calendar_year_company, fact, real_facts, yahoo

UTC = timezone.utc
NO_SPLITS = SplitReconciliation("NO_RECENT_SPLITS", ())


def contract(facts, yahoo_rows=(), *, as_of, splits=NO_SPLITS):
    view = build_quarterly_view(facts, yahoo_rows, split_events=splits.events, as_of=as_of)
    return build_c_contract_v3(view, splits, company_id=7, as_of=as_of)


def eight_quarters(eps, revenue):
    keys = [(2024, 1), (2024, 2), (2024, 3), (2025, 1), (2025, 2), (2025, 3)]
    return calendar_year_company(dict(zip(keys, eps)), revenue=dict(zip(keys, revenue)))


class ContractShapeTests(unittest.TestCase):
    def test_exact_eleven_inputs_and_version(self):
        result = contract(eight_quarters(["1"] * 6, ["10"] * 6), as_of=datetime(2025, 11, 15, tzinfo=UTC))
        for key in INDEPENDENT_C_INPUT_KEYS:
            self.assertIn(key, result)
        self.assertEqual(CONTRACT_VERSION, "c-input-contract-v3-11")
        self.assertEqual(result["c_input_contract"]["version"], CONTRACT_VERSION)
        self.assertEqual(set(result["c_input_contract"]["inputs"]), set(INDEPENDENT_C_INPUT_KEYS))

    def test_score_consumes_contract_unchanged(self):
        result = contract(eight_quarters(["1", "1", "1", "1.3", "1.4", "1.5"], ["10", "10", "10", "12", "13", "14"]),
                          as_of=datetime(2025, 11, 15, tzinfo=UTC))
        score = build_c_score(result)
        self.assertIn("c_score_v1", score)


class SemanticsTests(unittest.TestCase):
    def test_latest_and_previous_are_consecutive_quarters(self):
        result = contract(eight_quarters(["1", "1", "1", "1.3", "1.4", "1.5"], ["10"] * 6),
                          as_of=datetime(2025, 11, 15, tzinfo=UTC))
        self.assertAlmostEqual(result["latest_eps_yoy_pct"], 50.0)
        self.assertAlmostEqual(result["previous_eps_yoy_pct"], 40.0)
        self.assertAlmostEqual(result["eps_acceleration_pp"], 10.0)
        self.assertEqual(result["latest_eps"], 1.5)
        self.assertEqual([item["value"] for item in result["eps_yoy_pct"]], [30.0, 40.0, 50.0])

    def test_latest_quarter_without_yoy_is_none_not_stale(self):
        facts = eight_quarters(["1"] * 6, ["10"] * 6)
        rows = [yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2025, 12, 31), "2")]
        result = contract(facts, rows, as_of=datetime(2026, 2, 15, tzinfo=UTC))
        self.assertEqual(result["latest_eps"], 2.0)
        self.assertIsNone(result["latest_eps_yoy_pct"])
        self.assertIn("LATEST_QUARTER_YOY_UNAVAILABLE",
                      result["c_input_contract"]["inputs"]["latest_eps_yoy_pct"]["reasons"])

    def test_mixed_source_acceleration_is_flagged(self):
        facts = eight_quarters(["1"] * 6, ["10"] * 6)
        rows = [
            yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2024, 12, 31), "1"),
            yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2025, 12, 31), "2"),
        ]
        facts += [
            fact("EarningsPerShareDiluted", date(2024, 1, 1), date(2024, 12, 31), "4",
                 accession="k24", filed=date(2025, 2, 20), fy=2024, fp="FY", form="10-K"),
        ]
        result = contract(facts, rows, as_of=datetime(2026, 1, 20, tzinfo=UTC))
        self.assertAlmostEqual(result["latest_eps_yoy_pct"], 100.0)
        self.assertAlmostEqual(result["previous_eps_yoy_pct"], 0.0)
        self.assertIn("MIXED_SOURCE_ACCELERATION",
                      result["c_input_contract"]["inputs"]["eps_acceleration_pp"]["reasons"])

    def test_stale_latest_quarter_requires_review(self):
        result = contract(eight_quarters(["1"] * 6, ["10"] * 6), as_of=datetime(2026, 6, 1, tzinfo=UTC))
        self.assertEqual(result["data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("STALE_LATEST_QUARTER", result["integrity"]["diagnostics"])

    def test_split_reasons_from_view_force_review(self):
        facts = [
            fact("EarningsPerShareDiluted", date(2025, 4, 1), date(2025, 6, 30), "2.48",
                 accession="a", filed=date(2025, 7, 30), fy=2025, fp="Q2"),
            fact("EarningsPerShareDiluted", date(2025, 7, 1), date(2025, 9, 30), "0.30",
                 accession="b", filed=date(2025, 10, 30), fy=2025, fp="Q3"),
            fact("EarningsPerShareDiluted", date(2025, 4, 1), date(2025, 6, 30), "0.25",
                 accession="b", filed=date(2025, 10, 30), fy=2025, fp="Q3"),
        ]
        result = contract(facts, as_of=datetime(2025, 11, 15, tzinfo=UTC))
        self.assertEqual(result["split_integrity_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["data_integrity"], "REVIEW_REQUIRED")

    def test_provider_event_rejected_by_sec_leaves_no_recent_splits(self):
        from test_quarterly_v3 import SplitBasisTests

        event = SplitEvent(date(2021, 11, 15), Decimal("1.032"), ("YAHOO",))
        splits = SplitReconciliation("VERIFIED_ALREADY_ADJUSTED", (event,), ("PROVIDER_SPLIT_NOT_IN_SEC",))
        facts = SplitBasisTests().shares_pair("1000000", "1000000")
        result = contract(facts, as_of=datetime(2023, 1, 1, tzinfo=UTC), splits=splits)
        self.assertEqual(result["split_integrity_status"], "NO_RECENT_SPLITS")
        events = result["c_input_contract"]["inputs"]["split_integrity_status"]["events"]
        self.assertEqual(events[0]["status"], "REJECTED_BY_SEC")

    def test_foreign_filer_is_explicit(self):
        view = build_quarterly_view([], [], split_events=(), as_of=datetime(2026, 10, 2, tzinfo=UTC))
        result = build_c_contract_v3(view, NO_SPLITS, company_id=1, as_of=datetime(2026, 10, 2, tzinfo=UTC),
                                     filer_status="FOREIGN_FILER_NOT_SUPPORTED")
        self.assertEqual(result["data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("FOREIGN_FILER_NOT_SUPPORTED", result["integrity"]["diagnostics"])

    def test_registrant_succession_is_reported(self):
        from database.registrant_v3 import RegistrantLink

        link = RegistrantLink("0002115436", "0000034088", "8-K12B", "acc", date(2026, 7, 1), "LINKED")
        view = build_quarterly_view(eight_quarters(["1"] * 6, ["10"] * 6), [], split_events=(),
                                    as_of=datetime(2025, 11, 15, tzinfo=UTC))
        result = build_c_contract_v3(view, NO_SPLITS, company_id=1, as_of=datetime(2025, 11, 15, tzinfo=UTC),
                                     registrant_links=(link,))
        self.assertEqual(result["integrity"]["registrant_history"][0]["predecessor_cik"], "0000034088")
        self.assertIn("REGISTRANT_SUCCESSION:2026-07-01:0000034088->0002115436", result["integrity"]["diagnostics"])
        self.assertNotEqual(result["data_integrity"], "REVIEW_REQUIRED")

    def test_unknown_capture_is_unknown_and_review(self):
        result = contract(eight_quarters(["1"] * 6, ["10"] * 6), as_of=datetime(2025, 11, 15, tzinfo=UTC),
                          splits=SplitReconciliation("UNKNOWN", ()))
        self.assertEqual(result["split_integrity_status"], "UNKNOWN")
        self.assertEqual(result["data_integrity"], "REVIEW_REQUIRED")

    def test_provenance_names_source_concept_and_lineage(self):
        result = contract(eight_quarters(["1", "1", "1", "1.3", "1.4", "1.5"], ["10"] * 6),
                          as_of=datetime(2025, 11, 15, tzinfo=UTC))
        provenance = result["c_input_contract"]["inputs"]["latest_eps_yoy_pct"]
        self.assertEqual(provenance["source"], "SEC")
        self.assertEqual(provenance["concept"], "EarningsPerShareDiluted")
        self.assertEqual(provenance["fiscal_quarter"], "2025Q3")
        self.assertEqual(provenance["comparable_fiscal_quarter"], "2024Q3")
        self.assertTrue(provenance["lineage"])


class RealDataTests(unittest.TestCase):
    def test_aapl_baseline_score_is_reproduced(self):
        as_of = datetime(2026, 9, 3, tzinfo=UTC)
        view = build_quarterly_view(real_facts("aapl"), [], split_events=(), as_of=as_of)
        result = build_c_contract_v3(view, NO_SPLITS, company_id=1, as_of=as_of)
        self.assertAlmostEqual(result["latest_eps_yoy_pct"], 28.662420382, places=6)
        self.assertAlmostEqual(result["previous_eps_yoy_pct"], 21.818181818, places=6)
        self.assertAlmostEqual(result["latest_revenue_yoy_pct"], 16.356501765, places=6)
        self.assertAlmostEqual(result["previous_revenue_yoy_pct"], 16.595182416, places=6)
        self.assertEqual(result["latest_eps"], 2.02)
        score = build_c_score(result)["c_score_v1"]
        # Same eleven inputs as the frozen legacy baseline -> same score (62.69).
        self.assertEqual(score["normalized_score"], 62.69)

    def test_nvda_current_contract_uses_split_consistent_values(self):
        as_of = datetime(2026, 10, 2, tzinfo=UTC)
        splits = SplitReconciliation("VERIFIED_ALREADY_ADJUSTED", (
            SplitEvent(date(2021, 7, 20), Decimal("4"), ("SEC", "YAHOO")),
            SplitEvent(date(2024, 6, 10), Decimal("10"), ("SEC", "YAHOO")),
        ))
        view = build_quarterly_view(real_facts("nvda"), [], split_events=splits.events, as_of=as_of)
        result = build_c_contract_v3(view, splits, company_id=3, as_of=as_of)
        self.assertEqual(result["split_integrity_status"], "VERIFIED_ALREADY_ADJUSTED")
        self.assertAlmostEqual(result["latest_eps"], 2.46)
        self.assertAlmostEqual(result["latest_eps_yoy_pct"], 127.777777778, places=6)


if __name__ == "__main__":
    unittest.main()
