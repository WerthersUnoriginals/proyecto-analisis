"""Offline tests for the A input contract (a-input-contract-v1)."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.a_contract_v1 import A_INPUT_KEYS, CONTRACT_VERSION, build_a_contract_v1
from database.annual_v3 import build_annual_view
from database.split_basis import SplitEvent, SplitReconciliation
from test_annual_v3 import NVDA_EVENTS, annual_company
from test_quarterly_v3 import real_facts

UTC = timezone.utc
NO_SPLITS = SplitReconciliation("NO_RECENT_SPLITS", ())
AS_OF = datetime(2026, 3, 15, tzinfo=UTC)


def contract(facts, *, as_of=AS_OF, splits=NO_SPLITS):
    view = build_annual_view(facts, split_events=splits.events, as_of=as_of)
    return build_a_contract_v1(view, splits, company_id=1, as_of=as_of)


def healthy(**overrides):
    eps = {2021: "1.0", 2022: "1.3", 2023: "1.7", 2024: "2.2", 2025: "2.9"}
    revenue = {2021: "100", 2022: "120", 2023: "145", 2024: "175", 2025: "210"}
    net_income = {2021: "10", 2022: "13", 2023: "17", 2024: "22", 2025: "29"}
    equity = {2020: "50", 2021: "60", 2022: "70", 2023: "85", 2024: "100", 2025: "120"}
    values = dict(eps=eps, revenue=revenue, net_income=net_income, equity=equity)
    values.update(overrides)
    return annual_company(values.pop("eps"), **values)


class ShapeTests(unittest.TestCase):
    def test_all_inputs_and_version(self):
        result = contract(healthy())
        self.assertEqual(CONTRACT_VERSION, "a-input-contract-v1")
        for key in A_INPUT_KEYS:
            self.assertIn(key, result)
            self.assertIn(key, result["a_input_contract"]["inputs"])

    def test_healthy_company_values(self):
        result = contract(healthy())
        self.assertEqual(result["latest_fiscal_year"], 2025)
        self.assertEqual(result["latest_annual_eps"], 2.9)
        self.assertEqual(len(result["annual_eps_yoy_pct"]), 3)
        self.assertEqual(result["annual_eps_status"], ["GROWTH", "GROWTH", "GROWTH"])
        self.assertAlmostEqual(result["eps_cagr_3y_pct"], ((2.9 / 1.3) ** (1 / 3) - 1) * 100, places=6)
        self.assertEqual((result["down_years"], result["loss_years"]), (0, 0))
        self.assertAlmostEqual(result["roe_latest_pct"], 29 / 110 * 100, places=6)
        self.assertEqual(result["annual_data_integrity"], "VERIFIED")


class IntegrityTests(unittest.TestCase):
    def test_down_and_loss_years_are_counted(self):
        result = contract(healthy(eps={2021: "1", 2022: "1.3", 2023: "1.1", 2024: "-0.2", 2025: "0.5"}))
        self.assertEqual(result["down_years"], 2)
        self.assertEqual(result["loss_years"], 1)
        self.assertEqual(result["annual_eps_status"], ["GROWTH", "GROWTH", "LOSS_TO_PROFIT"])

    def test_non_meaningful_roe_requires_review(self):
        equity = {2020: "50", 2021: "60", 2022: "70", 2023: "85", 2024: "-10", 2025: "120"}
        result = contract(healthy(equity=equity))
        self.assertIsNone(result["roe_latest_pct"])
        self.assertEqual(result["annual_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("ROE_NOT_MEANINGFUL", result["integrity"]["diagnostics"])

    def test_stale_fiscal_year_requires_review(self):
        result = contract(healthy(), as_of=datetime(2027, 6, 1, tzinfo=UTC))
        self.assertEqual(result["annual_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("STALE_LATEST_FISCAL_YEAR", result["integrity"]["diagnostics"])

    def test_short_history_is_partial(self):
        facts = annual_company({2024: "1", 2025: "2"}, net_income={2024: "1", 2025: "2"},
                               equity={2023: "5", 2024: "5", 2025: "6"})
        result = contract(facts)
        self.assertIsNone(result["eps_cagr_3y_pct"])
        self.assertEqual(result["annual_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")

    def test_foreign_filer_is_explicit(self):
        view = build_annual_view([], split_events=(), as_of=AS_OF)
        result = build_a_contract_v1(view, NO_SPLITS, company_id=1, as_of=AS_OF,
                                     filer_status="FOREIGN_FILER_NOT_SUPPORTED")
        self.assertEqual(result["annual_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("FOREIGN_FILER_NOT_SUPPORTED", result["integrity"]["diagnostics"])

    def test_unresolved_succession_requires_review(self):
        from database.registrant_v3 import RegistrantLink

        link = RegistrantLink("0002115436", None, "8-K12B", "acc", date(2026, 7, 1), "PREDECESSOR_NOT_FOUND")
        view = build_annual_view(healthy(), split_events=(), as_of=AS_OF)
        result = build_a_contract_v1(view, NO_SPLITS, company_id=1, as_of=AS_OF, registrant_links=(link,))
        self.assertEqual(result["annual_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("REGISTRANT_SUCCESSION_UNRESOLVED", result["integrity"]["diagnostics"])

    def test_split_problems_require_review(self):
        result = contract(healthy(), splits=SplitReconciliation("UNKNOWN", ()))
        self.assertEqual(result["split_integrity_status"], "UNKNOWN")
        self.assertEqual(result["annual_data_integrity"], "REVIEW_REQUIRED")

    def test_provenance_names_years_tags_and_lineage(self):
        provenance = contract(healthy())["a_input_contract"]["inputs"]["eps_cagr_3y_pct"]
        self.assertEqual(provenance["fiscal_years"], [2022, 2025])
        self.assertEqual(provenance["concept"], "EarningsPerShareDiluted")
        self.assertTrue(provenance["lineage"])


class RealDataTests(unittest.TestCase):
    def test_nvda_contract(self):
        as_of = datetime(2026, 10, 2, tzinfo=UTC)
        splits = SplitReconciliation("VERIFIED_ALREADY_ADJUSTED", NVDA_EVENTS)
        result = contract(real_facts("nvda"), as_of=as_of, splits=splits)
        self.assertEqual(result["latest_fiscal_year"], 2026)
        self.assertEqual(result["annual_eps_status"], ["GROWTH", "GROWTH", "GROWTH"])
        self.assertAlmostEqual(result["annual_eps_yoy_pct"][0], 600.0, places=6)
        self.assertEqual(result["annual_data_integrity"], "VERIFIED")
        self.assertIn("ROE_ABOVE_100_PCT", result["integrity"]["diagnostics"])


if __name__ == "__main__":
    unittest.main()
