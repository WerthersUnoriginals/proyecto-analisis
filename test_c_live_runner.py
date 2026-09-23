"""Offline contract tests for the read-only live C diagnostic runner."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from database.c_live_diagnostic import diagnose_c_snapshots
from database.c_live_runner import (
    AcquisitionFailure,
    _default_load_persisted_rows,
    run_live_c_diagnostic,
)


ROOT = Path(__file__).resolve().parent
INPUT_KEYS = (
    "latest_eps_yoy_pct",
    "previous_eps_yoy_pct",
    "eps_acceleration_pp",
    "latest_revenue_yoy_pct",
    "previous_revenue_yoy_pct",
    "revenue_acceleration_pp",
    "latest_eps",
    "eps_yoy_pct",
    "eps_loss_to_profit",
)
EXPECTED_REPORT = {
    "latest_eps_yoy_pct": 50.0,
    "previous_eps_yoy_pct": 20.0,
    "eps_acceleration_pp": 30.0,
    "latest_revenue_yoy_pct": 20.0,
    "previous_revenue_yoy_pct": 10.0,
    "revenue_acceleration_pp": 10.0,
    "latest_eps": 3.0,
    "eps_yoy_pct": [
        {"date": "2025-03-31", "value": 20.0},
        {"date": "2025-06-30", "value": 50.0},
    ],
    "eps_loss_to_profit": False,
}


def _row(metric, day, value, row_id, *, source="SEC", variant=None, accession=True):
    variant = variant or (
        "sec.company_facts"
        if source == "SEC"
        else "yahoo.fundamentals_timeseries"
    )
    source_metric_name = {
        ("EPS_DILUTED", "SEC"): "EarningsPerShareDiluted",
        ("REVENUE", "SEC"): "RevenueFromContractWithCustomerExcludingAssessedTax",
        ("EPS_DILUTED", "YAHOO"): "quarterlyDilutedEPS",
        ("REVENUE", "YAHOO"): "quarterlyTotalRevenue",
    }[(metric, source)]
    unit = "USD/shares" if metric == "EPS_DILUTED" else "USD"
    observed = datetime(2025, 7, 1, 12, tzinfo=timezone.utc)
    row = {
        "company_id": 1,
        "metric": metric,
        "fiscal_year": day.year,
        "fiscal_quarter": 1 if day.month == 3 else 2,
        "canonical_period_end": day,
        "series_date": day,
        "value": Decimal(value),
        "unit": unit,
        "currency": "USD",
        "source": source,
        "source_variant": variant,
        "observation_kind": "REPORTED",
        "selected_observation_id": row_id + 1000,
        "raw_id": row_id,
        "source_metric_name": source_metric_name,
        "source_unit": unit,
        "source_scale_factor": Decimal("1"),
        "source_period_start": day - timedelta(days=89),
        "source_period_end": day,
        "filed_date": day + timedelta(days=30) if source == "SEC" else None,
        "source_available_at": (
            day + timedelta(days=30) if source == "SEC" else None
        ),
        "observed_at": observed,
        "normalizer_version": (
            "sec-normalized-v2" if source == "SEC" else "yahoo-normalized-v2"
        ),
        "intrinsic_quality_status": "OK",
        "selection_eligibility": "ELIGIBLE",
        "selection_policy_version": "c-v2.6-compatible-v1",
        "selection_reason": "TEST_EFFECTIVE",
        "comparison_rules_version": "sec-yahoo-comparison-v1",
        "comparison_status": "NOT_COMPARED",
        "comparison_reason": "NO_COMPARABLE",
        "comparison_reference_id": None,
        "comparison_difference_pct": None,
        "alignment_method": "EXACT",
        "alignment_days": 0,
        "alignment_reference_id": None,
    }
    if source == "SEC" and accession:
        row["source_record_id"] = f"0000000001-25-{row_id:06d}"
    return row


def _rows(*, source="SEC", variant=None, accession=True):
    values = (
        ("EPS_DILUTED", date(2024, 3, 31), "1", 1),
        ("EPS_DILUTED", date(2024, 6, 30), "2", 2),
        ("EPS_DILUTED", date(2025, 3, 31), "1.2", 3),
        ("EPS_DILUTED", date(2025, 6, 30), "3", 4),
        ("REVENUE", date(2024, 3, 31), "100", 5),
        ("REVENUE", date(2024, 6, 30), "200", 6),
        ("REVENUE", date(2025, 3, 31), "110", 7),
        ("REVENUE", date(2025, 6, 30), "240", 8),
    )
    return [
        _row(metric, day, value, row_id, source=source, variant=variant, accession=accession)
        for metric, day, value, row_id in values
    ]


def _evidence(row, **overrides):
    fields = (
        "company_id", "source", "source_variant", "metric",
        "source_metric_name", "fiscal_year", "fiscal_quarter",
        "canonical_period_end", "series_date", "source_period_start",
        "source_period_end", "filed_date", "source_available_at", "observed_at",
        "selected_observation_id", "raw_id", "unit", "source_unit",
        "currency", "source_scale_factor", "normalizer_version", "selection_policy_version",
        "selection_reason", "comparison_rules_version", "comparison_status",
        "comparison_reason", "comparison_reference_id",
        "comparison_difference_pct", "alignment_method", "alignment_days",
        "alignment_reference_id", "source_record_id", "source_identity_type",
    )
    result = {key: row[key] for key in fields if key in row}
    result.update(overrides)
    return result


def _lineage(rows):
    by_id = {row["selected_observation_id"]: _evidence(row) for row in rows}
    eps = [row for row in rows if row["metric"] == "EPS_DILUTED"]
    revenue = [row for row in rows if row["metric"] == "REVENUE"]
    eps_pairs = [
        [by_id[eps[2]["selected_observation_id"]], by_id[eps[0]["selected_observation_id"]]],
        [by_id[eps[3]["selected_observation_id"]], by_id[eps[1]["selected_observation_id"]]],
    ]
    revenue_pairs = [
        [by_id[revenue[2]["selected_observation_id"]], by_id[revenue[0]["selected_observation_id"]]],
        [by_id[revenue[3]["selected_observation_id"]], by_id[revenue[1]["selected_observation_id"]]],
    ]
    return {
        "latest_eps_yoy_pct": eps_pairs[1],
        "previous_eps_yoy_pct": eps_pairs[0],
        "eps_acceleration_pp": eps_pairs[1] + eps_pairs[0],
        "latest_revenue_yoy_pct": revenue_pairs[1],
        "previous_revenue_yoy_pct": revenue_pairs[0],
        "revenue_acceleration_pp": revenue_pairs[1] + revenue_pairs[0],
        "latest_eps": [eps_pairs[1][0]],
        "eps_yoy_pct": eps_pairs[0] + eps_pairs[1],
        "eps_loss_to_profit": eps_pairs[1],
    }


def _legacy_snapshot(rows=None, **overrides):
    rows = _rows() if rows is None else rows
    snapshot = {
        "fundamental_report": copy.deepcopy(EXPECTED_REPORT),
        "lineage_by_input": _lineage(rows),
        "shared_live_complements": {
            "data_integrity": "VERIFIED",
            "split_integrity_status": "NO_RECENT_SPLITS",
        },
        "legacy_end_to_end_score": {"c_score_v1": {"status": "COMPLETE"}},
        "provider_failures": [],
        "acquisition_status": "COMPLETE",
        "semantic_gaps": [
            {"code": "CORPORATE_ACTIONS_NOT_RECONSTRUCTED", "score_relevant": False},
            {"code": "DATA_INTEGRITY_NOT_RECONSTRUCTED", "score_relevant": True},
        ],
        "capture_metadata": {
            "legacy_capture_started_at": "2025-07-02T12:00:01+00:00",
            "legacy_capture_completed_at": "2025-07-02T12:00:02+00:00",
        },
    }
    snapshot.update(overrides)
    return snapshot


def _clock(*values):
    iterator = iter(values)
    return lambda: next(iterator)


class RunnerContractTests(unittest.TestCase):
    def test_exact_match_is_single_capture_read_only_and_keeps_end_to_end_open(self):
        calls = []
        rows = _rows()
        legacy = _legacy_snapshot(rows)

        def load(company_id):
            calls.append(("persisted", company_id))
            return copy.deepcopy(rows)

        def acquire(ticker, *, company_id, capture_clock):
            calls.append(("legacy", ticker, company_id, capture_clock is clock))
            return copy.deepcopy(legacy)

        def diagnose(*args, **kwargs):
            calls.append(("diagnose",))
            return diagnose_c_snapshots(*args, **kwargs)

        clock = _clock(
            datetime(2025, 7, 2, 12, 0, tzinfo=timezone.utc),
            datetime(2025, 7, 2, 12, 0, 1, tzinfo=timezone.utc),
            datetime(2025, 7, 2, 12, 0, 3, tzinfo=timezone.utc),
        )
        result = run_live_c_diagnostic(
            "TST", 1,
            acquire_legacy_snapshot=acquire,
            load_persisted_rows=load,
            diagnose_snapshots=diagnose,
            clock=clock,
        )

        self.assertEqual(calls, [
            ("persisted", 1),
            ("legacy", "TST", 1, True),
            ("diagnose",),
        ])
        self.assertEqual(result["legacy_live"], EXPECTED_REPORT)
        self.assertEqual(result["new_persisted"], EXPECTED_REPORT)
        self.assertTrue(result["fundamental_comparison"]["equivalent"])
        self.assertEqual(result["score_isolation"]["status"], "EQUIVALENT")
        self.assertFalse(result["end_to_end_equivalent"])
        self.assertEqual(result["end_to_end_status"], "NOT_ESTABLISHED")
        self.assertEqual(result["metadata"]["diagnostic_started_at"], "2025-07-02T12:00:00+00:00")
        self.assertEqual(result["metadata"]["persisted_read_at"], "2025-07-02T12:00:01+00:00")
        self.assertEqual(result["metadata"]["diagnostic_completed_at"], "2025-07-02T12:00:03+00:00")

    def test_raw_id_without_accession_never_becomes_strong_identity(self):
        rows = _rows(accession=False)
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: copy.deepcopy(rows),
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(_rows()),
        )

        alignment = result["evidence_alignment"]["latest_eps"]
        self.assertNotEqual(alignment["relation"], "SAME_EVIDENCE")
        self.assertNotIn("SAME_STRONG_IDENTITY", alignment["reason_codes"])
        persisted = result["classification"]["per_input"]["latest_eps"]
        self.assertNotEqual(persisted["category"], "POSSIBLE_REGRESSION")

    def test_same_real_sec_accession_can_establish_strong_identity(self):
        rows = _rows()
        legacy = _legacy_snapshot(rows)
        legacy["fundamental_report"]["latest_eps"] = 999.0
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: copy.deepcopy(rows),
            acquire_legacy_snapshot=lambda *args, **kwargs: legacy,
        )

        self.assertEqual(
            result["evidence_alignment"]["latest_eps"]["relation"],
            "SAME_EVIDENCE",
        )
        self.assertEqual(
            result["classification"]["per_input"]["latest_eps"]["category"],
            "POSSIBLE_REGRESSION",
        )

    def test_persisted_lineage_carries_source_identity_type_without_using_raw_id(self):
        rows = _rows()
        for row in rows:
            if row["source"] == "SEC":
                row["source_identity_type"] = "SEC_ACCESSION"
        evidence = _evidence(rows[0])
        self.assertEqual(evidence["source_identity_type"], "SEC_ACCESSION")
        self.assertNotEqual(evidence["source_identity_type"], "RAW_ID")

    def test_yahoo_without_immutable_revision_is_capture_time_difference(self):
        rows = _rows(source="YAHOO")
        legacy = _legacy_snapshot(rows)
        for evidence in legacy["lineage_by_input"]["latest_eps"]:
            evidence["observed_at"] = "2025-07-02T12:00:00+00:00"
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: copy.deepcopy(rows),
            acquire_legacy_snapshot=lambda *args, **kwargs: legacy,
        )

        alignment = result["evidence_alignment"]["latest_eps"]
        self.assertNotIn("STRONG", alignment["persisted_strengths"])
        self.assertEqual(
            result["classification"]["per_input"]["latest_eps"]["category"],
            "CAPTURE_TIME_DIFFERENCE",
        )

    def test_yahoo_variant_change_remains_source_drift(self):
        persisted = _rows(source="YAHOO")
        legacy_rows = _rows(
            source="YAHOO", variant="yfinance.quarterly_income_stmt",
        )
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: persisted,
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(legacy_rows),
        )

        self.assertEqual(
            result["classification"]["per_input"]["latest_eps"]["category"],
            "SOURCE_DRIFT",
        )

    def test_demonstrably_newer_legacy_quarter_is_expected_drift(self):
        legacy = _legacy_snapshot()
        for evidence in legacy["lineage_by_input"]["latest_eps"]:
            for field in (
                "canonical_period_end", "series_date", "source_period_start",
                "source_period_end", "filed_date", "source_available_at",
            ):
                evidence[field] = evidence[field] + timedelta(days=92)
            evidence["source_record_id"] += "-newer"
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: _rows(),
            acquire_legacy_snapshot=lambda *args, **kwargs: legacy,
        )

        self.assertEqual(
            result["classification"]["per_input"]["latest_eps"]["category"],
            "EXPECTED_DRIFT",
        )
        self.assertIn(
            "NEWER_LIVE_EVIDENCE",
            result["classification"]["per_input"]["latest_eps"]["reason_codes"],
        )

    def test_provider_failure_is_propagated_without_possible_regression(self):
        legacy = _legacy_snapshot(
            provider_failures=[{
                "provider": "YAHOO",
                "operation": "fundamentals_timeseries",
                "reason": "provider operation failed",
                "affects_comparability": True,
            }],
            acquisition_status="PARTIAL",
        )
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: _rows(),
            acquire_legacy_snapshot=lambda *args, **kwargs: legacy,
        )

        self.assertEqual(result["acquisition_status"]["run_status"], "PARTIAL")
        self.assertEqual(result["classification"]["primary"], "SEMANTIC_GAP")
        self.assertNotIn("POSSIBLE_REGRESSION", result["classification"]["categories_present"])

    def test_loader_exception_is_sanitized_failed_and_not_retried(self):
        calls = []

        def load(_):
            calls.append("load")
            raise AcquisitionFailure("postgres password=secret host=private")

        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=load,
            acquire_legacy_snapshot=lambda *args, **kwargs: (
                calls.append("legacy") or _legacy_snapshot()
            ),
        )

        self.assertEqual(calls, ["load", "legacy"])
        self.assertEqual(result["acquisition_status"]["persisted_status"], "FAILED")
        self.assertIn("DB_READ_FAILED", result["classification"]["reason_codes"])
        self.assertNotIn("secret", json.dumps(result))

    def test_default_loader_translates_only_known_configuration_and_driver_failures(self):
        with patch(
            "database.effective_fundamentals.load_effective_current",
            side_effect=RuntimeError(
                "Faltan variables de entorno para PostgreSQL: CANSLIM_DB_HOST"
            ),
        ):
            with self.assertRaises(AcquisitionFailure):
                _default_load_persisted_rows(1)

        with patch(
            "database.effective_fundamentals.load_effective_current",
            side_effect=RuntimeError("programming invariant"),
        ):
            with self.assertRaisesRegex(RuntimeError, "programming invariant"):
                _default_load_persisted_rows(1)

        with patch.dict(os.environ, {"CANSLIM_DB_PORT": "not-a-port"}):
            with patch(
                "database.effective_fundamentals.load_effective_current",
                side_effect=ValueError("invalid literal for int()"),
            ):
                with self.assertRaises(AcquisitionFailure):
                    _default_load_persisted_rows(1)

        driver_error = type("OperationalError", (Exception,), {
            "__module__": "psycopg.errors",
        })
        with patch(
            "database.effective_fundamentals.load_effective_current",
            side_effect=driver_error("connection refused"),
        ):
            with self.assertRaises(AcquisitionFailure):
                _default_load_persisted_rows(1)

    def test_zero_rows_and_incomplete_rows_are_distinct(self):
        empty = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: [],
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
        )
        incomplete = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: _rows()[:4],
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
        )

        self.assertEqual(empty["acquisition_status"]["persisted_status"], "FAILED")
        self.assertIn("NO_EFFECTIVE_ROWS", empty["classification"]["reason_codes"])
        self.assertEqual(incomplete["acquisition_status"]["persisted_status"], "PARTIAL")
        self.assertIn("PERSISTED_INPUTS_INCOMPLETE", incomplete["classification"]["reason_codes"])

    def test_legacy_exception_is_sanitized_and_programming_errors_propagate(self):
        def legacy_failure(*args, **kwargs):
            raise AcquisitionFailure("oauth token=secret")

        failed = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: _rows(),
            acquire_legacy_snapshot=legacy_failure,
        )
        self.assertEqual(failed["acquisition_status"]["legacy_status"], "FAILED")
        self.assertNotIn("secret", json.dumps(failed))

        malformed = _rows()
        del malformed[0]["metric"]
        with self.assertRaises(KeyError):
            run_live_c_diagnostic(
                "TST", 1,
                load_persisted_rows=lambda _: malformed,
                acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
            )

        with self.assertRaisesRegex(AssertionError, "loader programming bug"):
            run_live_c_diagnostic(
                "TST", 1,
                load_persisted_rows=lambda _: (_ for _ in ()).throw(
                    AssertionError("loader programming bug")
                ),
                acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
            )
        with self.assertRaisesRegex(AssertionError, "legacy programming bug"):
            run_live_c_diagnostic(
                "TST", 1,
                load_persisted_rows=lambda _: _rows(),
                acquire_legacy_snapshot=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("legacy programming bug")
                ),
            )

    def test_legacy_metadata_cannot_override_runner_authority(self):
        legacy = _legacy_snapshot()
        legacy["capture_metadata"].update({
            "ticker": "WRONG",
            "company_id": 999,
            "diagnostic_started_at": "1900-01-01T00:00:00+00:00",
            "persisted_read_at": "1900-01-01T00:00:00+00:00",
        })
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: _rows(),
            acquire_legacy_snapshot=lambda *args, **kwargs: legacy,
            clock=_clock(
                datetime(2025, 7, 2, 12, 0, tzinfo=timezone.utc),
                datetime(2025, 7, 2, 12, 1, tzinfo=timezone.utc),
                datetime(2025, 7, 2, 12, 2, tzinfo=timezone.utc),
            ),
        )

        self.assertEqual(result["metadata"]["ticker"], "TST")
        self.assertEqual(result["metadata"]["company_id"], 1)
        self.assertEqual(result["metadata"]["diagnostic_started_at"], "2025-07-02T12:00:00+00:00")
        self.assertEqual(result["metadata"]["persisted_read_at"], "2025-07-02T12:01:00+00:00")

    def test_realistic_legacy_shape_reports_missing_persisted_accession(self):
        persisted_rows = _rows(accession=False)
        legacy = _legacy_snapshot(_rows())
        for lineage in legacy["lineage_by_input"].values():
            for evidence in lineage:
                if "normalizer_version" in evidence:
                    evidence["origin_normalizer_version"] = evidence.pop(
                        "normalizer_version"
                    )
                for field in (
                    "canonical_period_end", "series_date", "source_period_start",
                    "source_period_end", "filed_date", "source_available_at",
                ):
                    if hasattr(evidence.get(field), "isoformat"):
                        evidence[field] = evidence[field].isoformat()
                evidence["currency"] = None
                evidence.pop("selected_observation_id", None)

        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: persisted_rows,
            acquire_legacy_snapshot=lambda *args, **kwargs: legacy,
        )

        self.assertNotEqual(
            result["evidence_alignment"]["latest_eps"]["relation"],
            "SAME_EVIDENCE",
        )
        self.assertIn(
            "PERSISTED_SEC_ACCESSION_NOT_EXPOSED",
            result["identity_assessment"]["limitations"],
        )
        self.assertTrue(result["identity_assessment"]["origin_metadata_preserved"])
        self.assertFalse(result["identity_assessment"]["raw_id_used_as_accession"])
        serialized = json.dumps(result)
        self.assertNotIn('"normalizer_version": "c-live-evidence-v1"', serialized)

    def test_partial_sec_accession_coverage_remains_an_explicit_limitation(self):
        persisted_rows = _rows(accession=False)
        persisted_rows[0]["source_record_id"] = "only-one-accession"
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: persisted_rows,
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
        )

        self.assertIn(
            "PERSISTED_SEC_ACCESSION_NOT_EXPOSED",
            result["identity_assessment"]["limitations"],
        )
        self.assertEqual(
            result["identity_assessment"]["sec_rows_without_accession"],
            7,
        )

    def test_shared_complements_and_unreconstructed_gaps_come_from_legacy(self):
        legacy = _legacy_snapshot()
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: _rows(),
            acquire_legacy_snapshot=lambda *args, **kwargs: legacy,
        )

        self.assertEqual(result["score_isolation"]["status"], "EQUIVALENT")
        codes = {gap["code"] for gap in result["semantic_gaps"]}
        self.assertEqual(codes, {
            "CORPORATE_ACTIONS_NOT_RECONSTRUCTED",
            "DATA_INTEGRITY_NOT_RECONSTRUCTED",
        })
        self.assertFalse(result["end_to_end_equivalent"])

    def test_freshness_uses_only_supplied_capture_and_evidence_timestamps(self):
        clock = _clock(
            datetime(2025, 7, 2, 12, 0, tzinfo=timezone.utc),
            datetime(2025, 7, 2, 12, 1, tzinfo=timezone.utc),
            datetime(2025, 7, 2, 12, 2, tzinfo=timezone.utc),
        )
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: _rows(),
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
            clock=clock,
        )

        freshness = result["freshness"]
        self.assertEqual(freshness["persisted_read_at"], "2025-07-02T12:01:00+00:00")
        self.assertEqual(freshness["persisted_snapshot_span"], {
            "start": "2025-07-01T12:00:00+00:00",
            "end": "2025-07-01T12:00:00+00:00",
        })
        yahoo_rows = _rows(source="YAHOO")
        yahoo = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: yahoo_rows,
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(yahoo_rows),
        )
        self.assertTrue(all(
            item["evidence_available_at"] is None
            for item in yahoo["freshness"]["evidence"]
        ))

    def test_same_inputs_and_clock_are_json_deterministic(self):
        def run():
            return run_live_c_diagnostic(
                "TST", 1,
                load_persisted_rows=lambda _: copy.deepcopy(_rows()),
                acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
                clock=_clock(
                    datetime(2025, 7, 2, 12, 0, tzinfo=timezone.utc),
                    datetime(2025, 7, 2, 12, 1, tzinfo=timezone.utc),
                    datetime(2025, 7, 2, 12, 2, tzinfo=timezone.utc),
                ),
            )

        first = json.dumps(run(), sort_keys=True, separators=(",", ":"))
        second = json.dumps(run(), sort_keys=True, separators=(",", ":"))
        self.assertEqual(first, second)


class RunnerSafetyTests(unittest.TestCase):
    def test_import_does_not_require_live_or_database_dependencies(self):
        script = r'''
import builtins
real_import = builtins.__import__
blocked = {"fundamental_c", "psycopg", "requests", "yfinance"}
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        raise AssertionError("live dependency imported: " + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import database.c_live_runner
print("IMPORT_SAFE")
'''
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "IMPORT_SAFE")

    def test_runner_source_contains_no_database_write_operation(self):
        source = (ROOT / "database" / "c_live_runner.py").read_text(encoding="utf-8")
        forbidden = ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "TRUNCATE ")
        self.assertFalse(any(token in source.upper() for token in forbidden))


if __name__ == "__main__":
    unittest.main()
