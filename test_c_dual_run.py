"""Offline behavioral tests for the two-level reproducible C dual-run."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.c_dual_run_contract import (
    COMPARISON_POLICY,
    load_aapl_dual_run_fixture,
)


MODULE_NAME = "database.c_dual_run"
MODULE_AVAILABLE = importlib.util.find_spec(MODULE_NAME) is not None
REPOSITORY_ROOT = Path(__file__).resolve().parent

if MODULE_AVAILABLE:
    from database.c_dual_run import (
        compare_c_dual_run,
        compare_fundamental_contract,
        compare_score_equivalence,
    )


class DualRunModulePresenceTests(unittest.TestCase):
    def test_dual_run_module_exists(self):
        self.assertTrue(MODULE_AVAILABLE, "database.c_dual_run must implement Task 10B.1")


@unittest.skipUnless(MODULE_AVAILABLE, "dual-run comparator not implemented yet")
class FundamentalContractComparisonTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_aapl_dual_run_fixture()
        self.baseline = copy.deepcopy(self.fixture["fundamental_contract"]["inputs"])

    def compare(self, new, **kwargs):
        return compare_fundamental_contract(self.baseline, new, **kwargs)

    def test_identical_values_are_exact_but_provenance_without_two_sides_is_not_comparable(self):
        result = compare_c_dual_run(self.baseline, copy.deepcopy(self.baseline), self.fixture)
        fundamental = result["fundamental_contract"]

        self.assertTrue(fundamental["value_equivalent"])
        self.assertIsNone(fundamental["provenance_equivalent"])
        self.assertFalse(fundamental["equivalent"])
        self.assertEqual(fundamental["provenance"]["status"], "NOT_COMPARABLE")
        self.assertTrue(all(item["status"] == "EXACT" for item in fundamental["fields"].values()))
        self.assertEqual(fundamental["differences"], [])

    def test_numeric_change_outside_tolerance_fails_values_and_score(self):
        new = copy.deepcopy(self.baseline)
        new["latest_eps_yoy_pct"] += 1.0

        result = compare_c_dual_run(self.baseline, new, self.fixture)
        field = result["fundamental_contract"]["fields"]["latest_eps_yoy_pct"]

        self.assertEqual(field["status"], "NUMERIC_DIFFERENCE")
        self.assertFalse(result["fundamental_contract"]["value_equivalent"])
        self.assertFalse(result["score"]["equivalent"])
        self.assertEqual(result["score"]["status"], "DIFFERENT")
        self.assertGreater(field["absolute_difference"], COMPARISON_POLICY["yoy_pct"]["abs_tol"])
        self.assertEqual(field["tolerance_used"], "yoy_pct")

    def test_numeric_change_inside_tolerance_is_equivalent_but_not_exact(self):
        new = copy.deepcopy(self.baseline)
        new["latest_eps_yoy_pct"] += 5e-9

        result = self.compare(new)
        field = result["fields"]["latest_eps_yoy_pct"]

        self.assertEqual(field["status"], "NUMERIC_EQUIVALENT")
        self.assertTrue(field["equivalent"])
        self.assertTrue(result["value_equivalent"])

    def test_eps_history_date_change_is_a_date_difference(self):
        new = copy.deepcopy(self.baseline)
        new["eps_yoy_pct"][2]["date"] = "2025-12-26"

        result = self.compare(new)

        self.assertFalse(result["value_equivalent"])
        self.assertIn(
            "DATE_DIFFERENCE",
            {item["status"] for item in result["differences"]},
        )
        self.assertEqual(
            result["fields"]["eps_yoy_pct"]["records"][2]["date"]["status"],
            "DATE_DIFFERENCE",
        )

    def test_eps_history_order_is_contractual_and_never_silently_sorted(self):
        new = copy.deepcopy(self.baseline)
        new["eps_yoy_pct"][1], new["eps_yoy_pct"][2] = (
            new["eps_yoy_pct"][2],
            new["eps_yoy_pct"][1],
        )

        result = self.compare(new)

        self.assertFalse(result["value_equivalent"])
        self.assertTrue(any(
            item["status"] == "SEMANTIC_DIFFERENCE"
            and item.get("reason") == "ORDER_DIFFERENCE"
            for item in result["differences"]
        ))

    def test_missing_field_and_loss_to_profit_change_are_explicit(self):
        missing = copy.deepcopy(self.baseline)
        del missing["previous_revenue_yoy_pct"]
        missing_result = self.compare(missing)
        self.assertEqual(
            missing_result["fields"]["previous_revenue_yoy_pct"]["status"],
            "MISSING_NEW",
        )
        self.assertFalse(missing_result["value_equivalent"])

        changed = copy.deepcopy(self.baseline)
        changed["eps_loss_to_profit"] = True
        changed_result = compare_c_dual_run(self.baseline, changed, self.fixture)
        self.assertEqual(
            changed_result["fundamental_contract"]["fields"]["eps_loss_to_profit"]["status"],
            "SEMANTIC_DIFFERENCE",
        )
        self.assertFalse(changed_result["score"]["equivalent"])
        self.assertEqual(changed_result["score"]["flags_extra"], ["LOSS_TO_PROFIT"])

    def test_none_and_key_presence_are_not_conflated(self):
        legacy = copy.deepcopy(self.baseline)
        new = copy.deepcopy(self.baseline)
        legacy["previous_revenue_yoy_pct"] = None
        new["previous_revenue_yoy_pct"] = None
        self.assertEqual(
            compare_fundamental_contract(legacy, new)["fields"]["previous_revenue_yoy_pct"]["status"],
            "EXACT",
        )
        del new["previous_revenue_yoy_pct"]
        self.assertEqual(
            compare_fundamental_contract(legacy, new)["fields"]["previous_revenue_yoy_pct"]["status"],
            "MISSING_NEW",
        )

    def test_two_explicit_provenances_compare_separately_from_values(self):
        legacy_lineage = copy.deepcopy(self.fixture["provenance"]["input_lineage"])
        new_lineage = copy.deepcopy(legacy_lineage)
        equal = compare_fundamental_contract(
            self.baseline,
            copy.deepcopy(self.baseline),
            legacy_provenance=legacy_lineage,
            new_provenance=new_lineage,
        )
        self.assertTrue(equal["provenance_equivalent"])
        self.assertTrue(equal["equivalent"])

        new_lineage["latest_eps_yoy_pct"]["source"] = "YAHOO"
        different = compare_fundamental_contract(
            self.baseline,
            copy.deepcopy(self.baseline),
            legacy_provenance=legacy_lineage,
            new_provenance=new_lineage,
        )
        self.assertTrue(different["value_equivalent"])
        self.assertFalse(different["provenance_equivalent"])
        self.assertFalse(different["equivalent"])
        self.assertIn(
            "SOURCE_DIFFERENCE",
            {item["status"] for item in different["provenance"]["differences"]},
        )

    def test_one_sided_or_partial_provenance_is_not_comparable(self):
        lineage = copy.deepcopy(self.fixture["provenance"]["input_lineage"])
        one_sided = compare_c_dual_run(
            self.baseline,
            copy.deepcopy(self.baseline),
            self.fixture,
            legacy_provenance=lineage,
        )
        self.assertIsNone(one_sided["fundamental_contract"]["provenance_equivalent"])
        self.assertEqual(
            one_sided["fundamental_contract"]["provenance"]["status"],
            "NOT_COMPARABLE",
        )

        partial = {"latest_eps": lineage["latest_eps"]}
        partial_result = compare_fundamental_contract(
            self.baseline,
            copy.deepcopy(self.baseline),
            legacy_provenance=partial,
            new_provenance=copy.deepcopy(partial),
        )
        self.assertIsNone(partial_result["provenance_equivalent"])
        self.assertEqual(partial_result["provenance"]["status"], "NOT_COMPARABLE")
        self.assertIn("previous_eps_yoy_pct", partial_result["provenance"]["missing_fields"])


@unittest.skipUnless(MODULE_AVAILABLE, "dual-run comparator not implemented yet")
class ScoreEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_aapl_dual_run_fixture()
        self.inputs = copy.deepcopy(self.fixture["fundamental_contract"]["inputs"])

    def test_aapl_level_two_matches_complete_contractual_score(self):
        result = compare_c_dual_run(self.inputs, copy.deepcopy(self.inputs), self.fixture)
        score = result["score"]
        legacy = score["legacy"]
        expected = self.fixture["expected_score"]

        self.assertTrue(score["equivalent"])
        self.assertEqual(score["status"], "EQUIVALENT")
        self.assertEqual(legacy["c_score_v1"]["normalized_score"], 62.69)
        self.assertEqual(legacy["c_score_v1"]["raw_points"], 59.55)
        self.assertEqual(legacy["c_score_v1"]["available_points"], 95.0)
        self.assertEqual(legacy["c_score_v1"]["class"], "ACCEPTABLE")
        self.assertEqual(legacy["c_score_v1"]["status"], "OK")
        self.assertEqual(legacy["c_score_v1"]["usability"], "C_SCORE_USABLE")
        self.assertEqual(legacy["c_flags"], [])
        self.assertEqual(legacy["c_score_v1"]["diagnostic"], [])
        self.assertEqual(legacy["c_classic"]["result"], "PASS")
        self.assertEqual(score["component_differences"], [])
        for name, component in expected["components"].items():
            actual = legacy["c_score_v1"]["components"][name]
            if component["points"] is None:
                self.assertIsNone(actual["points"])
            else:
                self.assertLessEqual(
                    abs(actual["points"] - component["points"]),
                    COMPARISON_POLICY["component_points"]["abs_tol"],
                )

    def test_invalid_shared_complement_contract_fails_before_scoring(self):
        invalid = copy.deepcopy(self.fixture)
        invalid["shared_complements"]["shared_for_score_isolation"] = False
        with self.assertRaisesRegex(ValueError, "shared_for_score_isolation"):
            compare_c_dual_run(self.inputs, copy.deepcopy(self.inputs), invalid)

        invalid = copy.deepcopy(self.fixture)
        invalid["shared_complements"]["independently_reconstructed_by_new_architecture"] = True
        with self.assertRaisesRegex(ValueError, "independently_reconstructed"):
            compare_c_dual_run(self.inputs, copy.deepcopy(self.inputs), invalid)

    def test_both_score_branches_receive_the_same_contractual_complements(self):
        observed = []

        def capture(report):
            observed.append(copy.deepcopy(report))
            from c_score_v1 import build_c_score
            return build_c_score(report)

        with patch("database.c_dual_run.build_c_score", side_effect=capture):
            compare_c_dual_run(self.inputs, copy.deepcopy(self.inputs), self.fixture)

        self.assertEqual(len(observed), 2)
        for key, value in self.fixture["shared_complements"]["values"].items():
            self.assertEqual(observed[0][key], value)
            self.assertEqual(observed[1][key], value)
        self.assertIsNot(observed[0], observed[1])

    def test_same_final_score_cannot_hide_a_component_difference(self):
        from c_score_v1 import build_c_score

        report = {**self.inputs, **self.fixture["shared_complements"]["values"]}
        legacy_score = build_c_score(report)
        new_score = copy.deepcopy(legacy_score)
        new_score["c_score_v1"]["components"]["eps_growth"]["points"] += 0.5
        self.assertEqual(
            legacy_score["c_score_v1"]["normalized_score"],
            new_score["c_score_v1"]["normalized_score"],
        )

        with patch(
            "database.c_dual_run.build_c_score",
            side_effect=[legacy_score, new_score],
        ):
            result = compare_score_equivalence(self.inputs, self.inputs, self.fixture)

        self.assertFalse(result["equivalent"])
        self.assertEqual(len(result["component_differences"]), 1)
        self.assertEqual(result["component_differences"][0]["component"], "eps_growth")

    def test_unscorable_none_history_is_reported_without_false_equivalence(self):
        legacy = copy.deepcopy(self.inputs)
        new = copy.deepcopy(self.inputs)
        legacy["eps_yoy_pct"] = None
        new["eps_yoy_pct"] = None

        result = compare_c_dual_run(legacy, new, self.fixture)

        self.assertTrue(result["fundamental_contract"]["value_equivalent"])
        self.assertFalse(result["score"]["equivalent"])
        self.assertEqual(result["score"].get("status"), "SCORE_EXECUTION_ERROR")
        self.assertEqual(result["score"]["legacy"], None)
        self.assertEqual(result["score"]["new"], None)
        self.assertEqual(
            [item["side"] for item in result["score"]["errors"]],
            ["legacy", "new"],
        )
        self.assertTrue(all(item["type"] == "TypeError" for item in result["score"]["errors"]))


@unittest.skipUnless(MODULE_AVAILABLE, "dual-run comparator not implemented yet")
class GlobalContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_aapl_dual_run_fixture()
        self.inputs = copy.deepcopy(self.fixture["fundamental_contract"]["inputs"])

    def test_pass_does_not_erase_semantic_gaps_or_claim_end_to_end_equivalence(self):
        result = compare_c_dual_run(self.inputs, copy.deepcopy(self.inputs), self.fixture)

        self.assertTrue(result["fundamental_contract"]["value_equivalent"])
        self.assertTrue(result["score"]["equivalent"])
        self.assertEqual(result["semantic_gaps"], self.fixture["semantic_gaps"])
        self.assertFalse(result["end_to_end_equivalent"])
        self.assertEqual(result["end_to_end_status"], "NOT_ESTABLISHED")

    def test_repeated_runs_are_byte_for_byte_json_deterministic(self):
        first = compare_c_dual_run(self.inputs, copy.deepcopy(self.inputs), self.fixture)
        second = compare_c_dual_run(self.inputs, copy.deepcopy(self.inputs), self.fixture)
        self.assertEqual(
            json.dumps(first, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            json.dumps(second, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )

    def test_module_imports_and_runs_without_network_or_database_dependencies(self):
        code = """
import builtins
blocked = {'requests', 'yfinance', 'psycopg'}
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.', 1)[0] in blocked or name == 'database.db':
        raise AssertionError('forbidden import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from database.c_dual_run_contract import load_aapl_dual_run_fixture
from database.c_dual_run import compare_c_dual_run
fixture = load_aapl_dual_run_fixture()
inputs = fixture['fundamental_contract']['inputs']
result = compare_c_dual_run(inputs, inputs, fixture)
assert result['score']['equivalent'] is True
print('OFFLINE_OK')
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
        with tempfile.TemporaryDirectory() as workdir:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=workdir,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "OFFLINE_OK")


if __name__ == "__main__":
    unittest.main()
