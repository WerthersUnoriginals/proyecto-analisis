"""Offline contract tests for the reproducible C dual-run fixture."""

import importlib.util
import json
import unittest
from pathlib import Path


MODULE_NAME = "database.c_dual_run_contract"
MODULE_AVAILABLE = importlib.util.find_spec(MODULE_NAME) is not None

if MODULE_AVAILABLE:
    from database.c_dual_run_contract import (
        COMPARISON_POLICY,
        CONTRACT_VERSION,
        FIXTURE_VERSION,
        FUNDAMENTAL_INPUT_KEYS,
        TOLERANCE_CONTRACT_VERSION,
        load_aapl_dual_run_fixture,
    )


REPOSITORY_ROOT = Path(__file__).resolve().parent
LEGACY_BASELINE_PATH = REPOSITORY_ROOT / "fixtures" / "aapl_c_v26_baseline.json"
EXPECTED_INPUT_KEYS = {
    "latest_eps_yoy_pct",
    "previous_eps_yoy_pct",
    "eps_acceleration_pp",
    "latest_revenue_yoy_pct",
    "previous_revenue_yoy_pct",
    "revenue_acceleration_pp",
    "latest_eps",
    "eps_yoy_pct",
    "eps_loss_to_profit",
}


class ContractModulePresenceTests(unittest.TestCase):
    def test_contract_module_exists(self):
        self.assertTrue(
            MODULE_AVAILABLE,
            "database.c_dual_run_contract must define the versioned contract",
        )


@unittest.skipUnless(MODULE_AVAILABLE, "contract module not implemented yet")
class DualRunContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_aapl_dual_run_fixture()
        cls.legacy = json.loads(LEGACY_BASELINE_PATH.read_text(encoding="utf-8"))

    def test_fixture_identity_and_top_level_sections_are_unambiguous(self):
        self.assertEqual(self.fixture["contract_version"], CONTRACT_VERSION)
        self.assertEqual(self.fixture["fixture_version"], FIXTURE_VERSION)
        self.assertEqual(self.fixture["ticker"], "AAPL")
        self.assertEqual(self.fixture["company_id"], 1)
        self.assertEqual(self.fixture["baseline_date"], "2026-09-03")
        self.assertEqual(
            set(self.fixture),
            {
                "contract_version",
                "fixture_version",
                "ticker",
                "company_id",
                "baseline_date",
                "fundamental_contract",
                "shared_complements",
                "expected_score",
                "provenance",
                "semantic_gaps",
            },
        )

    def test_fundamental_contract_contains_exactly_the_nine_task_10a_inputs(self):
        block = self.fixture["fundamental_contract"]
        self.assertEqual(set(block), {"inputs"})
        self.assertEqual(set(block["inputs"]), EXPECTED_INPUT_KEYS)
        self.assertEqual(set(FUNDAMENTAL_INPUT_KEYS), EXPECTED_INPUT_KEYS)
        self.assertNotIn("data_integrity", block["inputs"])
        self.assertNotIn("split_integrity_status", block["inputs"])
        self.assertEqual(
            {
                key: block["inputs"][key]
                for key in (
                    "latest_eps_yoy_pct",
                    "previous_eps_yoy_pct",
                    "eps_acceleration_pp",
                    "latest_revenue_yoy_pct",
                    "previous_revenue_yoy_pct",
                    "revenue_acceleration_pp",
                )
            },
            {
                "latest_eps_yoy_pct": 28.662420382165607,
                "previous_eps_yoy_pct": 21.818181818181817,
                "eps_acceleration_pp": 6.844238563983787,
                "latest_revenue_yoy_pct": 16.356501765281383,
                "previous_revenue_yoy_pct": 16.595182415922984,
                "revenue_acceleration_pp": -0.23868065064160418,
            },
        )
        self.assertEqual(block["inputs"]["latest_eps"], 2.02)
        self.assertFalse(block["inputs"]["eps_loss_to_profit"])
        self.assertEqual(
            block["inputs"]["eps_yoy_pct"],
            [
                {"date": "2025-03-29", "value": 7.8431372549019605},
                {"date": "2025-06-28", "value": 12.142857142857142},
                {"date": "2025-12-27", "value": 18.333333333333332},
                {"date": "2026-03-28", "value": 21.818181818181817},
                {"date": "2026-06-27", "value": 28.662420382165607},
            ],
        )

    def test_shared_complements_are_explicit_and_separate(self):
        complements = self.fixture["shared_complements"]
        self.assertTrue(complements["shared_for_score_isolation"])
        self.assertFalse(complements["independently_reconstructed_by_new_architecture"])
        self.assertEqual(
            complements["values"],
            {
                "data_integrity": "VERIFIED",
                "split_integrity_status": "NO_RECENT_SPLITS",
            },
        )
        self.assertEqual(complements["source"], "legacy_baseline")

    def test_semantic_gaps_are_machine_readable(self):
        gaps = self.fixture["semantic_gaps"]
        self.assertEqual(
            gaps,
            {
                "data_integrity": "SHARED_LEGACY_COMPLEMENT",
                "split_integrity_status": "SHARED_LEGACY_COMPLEMENT",
                "splits_persisted_in_new_architecture": False,
                "new_quality_semantics_equal_legacy_data_integrity": False,
                "proves_end_to_end_integrity_split_equivalence": False,
            },
        )

    def test_expected_score_is_the_complete_legacy_baseline(self):
        score = self.fixture["expected_score"]
        self.assertEqual(score["raw_points"], 59.55)
        self.assertEqual(score["available_points"], 95.0)
        self.assertEqual(score["normalized_score"], 62.69)
        self.assertEqual(score["class"], "ACCEPTABLE")
        self.assertEqual(score["status"], "OK")
        self.assertEqual(score["usability"], "C_SCORE_USABLE")
        self.assertEqual(score["c_flags"], [])
        self.assertEqual(score["diagnostic"], [])
        self.assertEqual(
            score["classic"],
            {
                "eps_yoy_ge_25": True,
                "sales_growth_strong": False,
                "eps_accelerating": True,
                "sales_accelerating": False,
                "result": "PASS",
            },
        )
        self.assertEqual(
            {name: item["points"] for name, item in score["components"].items()},
            {
                "eps_growth": 21.46496815286624,
                "eps_acceleration": 7.368847712796759,
                "eps_trend_quality": 7.0,
                "sales_growth": 12.81390105916883,
                "sales_acceleration": 5.904527739743356,
                "persistence": 5.0,
                "eps_surprise": None,
            },
        )
        self.assertFalse(score["components"]["eps_surprise"]["available"])
        self.assertEqual(score["components"]["eps_surprise"]["status"], "UNVERIFIED")

    def test_tolerances_are_versioned_and_match_the_approved_policy(self):
        self.assertEqual(TOLERANCE_CONTRACT_VERSION, "c-dual-run-tolerances-v1")
        self.assertEqual(
            COMPARISON_POLICY,
            {
                "eps": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-7},
                "large_monetary": {"mode": "numeric", "abs_tol": 0.01, "rel_tol": 1e-10},
                "yoy_pct": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
                "acceleration_pp": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
                "component_points": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
                "raw_points": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
                "available_points": {"mode": "exact"},
                "normalized_score": {"mode": "exact"},
                "dates": {"mode": "exact"},
                "source": {"mode": "exact"},
                "source_variant": {"mode": "exact"},
                "none_and_key_presence": {"mode": "exact"},
                "class_status_usability": {"mode": "exact"},
                "flags": {"mode": "exact_set"},
                "diagnostic": {"mode": "exact"},
                "classic": {"mode": "exact"},
            },
        )

    def test_fixture_score_and_complements_match_the_legacy_fixture(self):
        expected = self.fixture["expected_score"]
        legacy_score = self.legacy["c_score"]
        for key in (
            "model_version",
            "raw_points",
            "available_points",
            "normalized_score",
            "class",
            "status",
            "usability",
            "components",
            "diagnostic",
            "surprise_policy",
            "low_persistence_policy",
            "small_base",
        ):
            self.assertEqual(expected[key], legacy_score[key])
        self.assertEqual(
            self.fixture["shared_complements"]["values"],
            {
                "data_integrity": self.legacy["report"]["data_integrity"],
                "split_integrity_status": self.legacy["report"]["split_integrity_status"],
            },
        )

    def test_provenance_records_legacy_and_effective_sources_without_external_lookup(self):
        provenance = self.fixture["provenance"]
        self.assertEqual(
            provenance["legacy_baseline"]["path"],
            "fixtures/aapl_c_v26_baseline.json",
        )
        self.assertEqual(provenance["legacy_baseline"]["algorithm_version"], "2.6-exp")
        self.assertEqual(provenance["legacy_baseline"]["score_model_version"], "1.2-exp")
        self.assertEqual(
            provenance["effective_fundamentals"]["selection_policy_version"],
            "c-v2.6-compatible-v1",
        )
        self.assertEqual(
            {item["source"] for item in provenance["input_lineage"].values()},
            {"SEC"},
        )
        self.assertEqual(
            {item["source_variant"] for item in provenance["input_lineage"].values()},
            {"sec.company_facts"},
        )
        self.assertEqual(
            provenance["input_lineage"],
            {
                "latest_eps_yoy_pct": {"current_date": "2026-06-27", "comparable_date": "2025-06-28", "source": "SEC", "source_variant": "sec.company_facts"},
                "previous_eps_yoy_pct": {"current_date": "2026-03-28", "comparable_date": "2025-03-29", "source": "SEC", "source_variant": "sec.company_facts"},
                "eps_acceleration_pp": {"current_date": "2026-06-27", "previous_date": "2026-03-28", "source": "SEC", "source_variant": "sec.company_facts"},
                "latest_revenue_yoy_pct": {"current_date": "2026-06-27", "comparable_date": "2025-06-28", "source": "SEC", "source_variant": "sec.company_facts"},
                "previous_revenue_yoy_pct": {"current_date": "2026-03-28", "comparable_date": "2025-03-29", "source": "SEC", "source_variant": "sec.company_facts"},
                "revenue_acceleration_pp": {"current_date": "2026-06-27", "previous_date": "2026-03-28", "source": "SEC", "source_variant": "sec.company_facts"},
                "latest_eps": {"current_date": "2026-06-27", "source": "SEC", "source_variant": "sec.company_facts"},
                "eps_yoy_pct": {"first_date": "2025-03-29", "last_date": "2026-06-27", "source": "SEC", "source_variant": "sec.company_facts"},
                "eps_loss_to_profit": {"current_date": "2026-06-27", "comparable_date": "2025-06-28", "source": "SEC", "source_variant": "sec.company_facts"},
            },
        )


if __name__ == "__main__":
    unittest.main()
