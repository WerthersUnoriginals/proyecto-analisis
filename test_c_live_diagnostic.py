import copy
import json
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone

from database.c_dual_run_contract import (
    FUNDAMENTAL_INPUT_KEYS,
    load_aapl_dual_run_fixture,
)
from database.c_live_diagnostic import (
    _compare_input_lineage,
    _final_end_to_end_gate,
    calculate_freshness,
    compare_evidence_lineage,
    diagnose_c_snapshots,
)


def _report():
    return copy.deepcopy(load_aapl_dual_run_fixture()["fundamental_contract"]["inputs"])


def _sec_evidence(**overrides):
    evidence = {
        "company_id": 1,
        "source": "SEC",
        "source_variant": "sec.company_facts",
        "metric": "EPS_DILUTED",
        "source_metric_name": "EarningsPerShareDiluted",
        "fiscal_year": 2025,
        "fiscal_quarter": 1,
        "canonical_period_end": "2025-03-29",
        "source_period_start": "2024-12-29",
        "source_period_end": "2025-03-29",
        "filed_date": "2025-05-02",
        "source_available_at": None,
        "observed_at": "2025-05-03T12:00:00+00:00",
        "raw_id": 101,
        "source_record_id": "0000320193-25-000057",
        "normalizer_version": "sec-normalized-v2",
        "selection_policy_version": "c-v2.6-compatible-v1",
        "unit": "USD/shares",
        "source_scale_factor": "1",
        "currency": "USD",
    }
    evidence.update(overrides)
    return evidence


def _yahoo_evidence(**overrides):
    evidence = {
        "company_id": 1,
        "source": "YAHOO",
        "source_variant": "yahoo.fundamentals_timeseries",
        "metric": "EPS_DILUTED",
        "source_metric_name": "quarterlyDilutedEPS",
        "fiscal_year": 2025,
        "fiscal_quarter": 1,
        "canonical_period_end": "2025-03-29",
        "source_period_start": None,
        "source_period_end": "2025-03-29",
        "filed_date": None,
        "source_available_at": None,
        "observed_at": "2025-05-03T12:00:00+00:00",
        "raw_id": 201,
        "provider_id": "quarterlyDilutedEPS:2025-03-29",
        "normalizer_version": "yahoo-normalized-v2",
        "selection_policy_version": "c-v2.6-compatible-v1",
        "unit": "USD/shares",
        "source_scale_factor": "1",
        "currency": "USD",
    }
    evidence.update(overrides)
    return evidence


def _lineage(evidence=None):
    base = evidence or _sec_evidence()
    history_count = len(_report()["eps_yoy_pct"])
    counts = {
        "latest_eps_yoy_pct": 2,
        "previous_eps_yoy_pct": 2,
        "eps_acceleration_pp": 4,
        "latest_revenue_yoy_pct": 2,
        "previous_revenue_yoy_pct": 2,
        "revenue_acceleration_pp": 4,
        "latest_eps": 1,
        "eps_yoy_pct": history_count * 2,
        "eps_loss_to_profit": 2,
    }
    result = {}
    for field, count in counts.items():
        items = []
        for index in range(count):
            item = copy.deepcopy(base)
            if count > 1:
                immutable = item.get("source_record_id")
                if immutable is not None:
                    item["source_record_id"] = f"{immutable}:{field}:{index}"
                item["raw_id"] = item.get("raw_id", 0) * 100 + index
            items.append(item)
        result[field] = items
    return result


def _snapshot(report=None, lineage=None, **overrides):
    snapshot = {
        "fundamental_report": _report() if report is None else report,
        "lineage_by_input": _lineage() if lineage is None else lineage,
        "capture_metadata": {},
        "acquisition_status": "COMPLETE",
        "provider_failures": [],
        "semantic_gaps": [],
    }
    snapshot.update(overrides)
    return snapshot


CAPTURE_METADATA = {
    "persisted_read_at": "2025-05-05T12:00:00+00:00",
    "legacy_capture_started_at": "2025-05-05T12:01:00+00:00",
    "legacy_capture_completed_at": "2025-05-05T12:02:00+00:00",
}


class ClassificationTests(unittest.TestCase):
    def _diagnose(self, legacy=None, persisted=None, **kwargs):
        return diagnose_c_snapshots(
            _snapshot() if legacy is None else legacy,
            _snapshot() if persisted is None else persisted,
            capture_metadata=CAPTURE_METADATA,
            **kwargs,
        )

    def test_exact_same_strong_evidence_is_match(self):
        result = self._diagnose()

        self.assertEqual(result["classification"]["primary"], "MATCH")
        self.assertEqual(result["severity"], "INFO")
        self.assertEqual(
            result["evidence_alignment"]["latest_eps"]["relation"],
            "SAME_EVIDENCE",
        )
        self.assertIn(
            "SAME_STRONG_IDENTITY",
            result["evidence_alignment"]["latest_eps"]["reason_codes"],
        )

    def test_same_strong_evidence_with_numeric_noise_is_numeric_equivalent(self):
        persisted_report = _report()
        persisted_report["latest_eps_yoy_pct"] += 5e-9

        result = self._diagnose(persisted=_snapshot(report=persisted_report))

        self.assertEqual(result["classification"]["primary"], "NUMERIC_EQUIVALENT")
        self.assertIn(
            "VALUE_WITHIN_TOLERANCE",
            result["classification"]["reason_codes"],
        )

    def test_same_strong_evidence_outside_tolerance_is_possible_regression(self):
        persisted_report = _report()
        persisted_report["latest_eps_yoy_pct"] += 1.0

        result = self._diagnose(persisted=_snapshot(report=persisted_report))

        self.assertEqual(result["classification"]["primary"], "POSSIBLE_REGRESSION")
        self.assertEqual(result["severity"], "REVIEW_REQUIRED")
        self.assertIn(
            "VALUE_OUTSIDE_TOLERANCE",
            result["classification"]["reason_codes"],
        )

    def test_newer_live_sec_filing_is_expected_drift(self):
        live = _sec_evidence(
            source_record_id="0000320193-25-000099",
            filed_date="2025-05-10",
            observed_at="2025-05-11T12:00:00+00:00",
            raw_id=999,
        )
        result = self._diagnose(legacy=_snapshot(lineage=_lineage(live)))

        self.assertEqual(result["classification"]["primary"], "EXPECTED_DRIFT")
        self.assertIn("NEWER_LIVE_EVIDENCE", result["classification"]["reason_codes"])

    def test_newer_live_quarter_is_expected_drift(self):
        live = _sec_evidence(
            source_record_id="new-quarter",
            fiscal_quarter=2,
            canonical_period_end="2025-06-28",
            source_period_start="2025-03-30",
            source_period_end="2025-06-28",
            filed_date="2025-08-01",
            observed_at="2025-08-02T12:00:00+00:00",
            raw_id=998,
        )
        result = self._diagnose(legacy=_snapshot(lineage=_lineage(live)))

        self.assertEqual(result["classification"]["primary"], "EXPECTED_DRIFT")

    def test_mixed_newness_in_a_derived_chain_is_not_expected_drift(self):
        legacy_lineage = _lineage()
        persisted_lineage = _lineage()
        legacy_lineage["latest_eps_yoy_pct"] = [
            _sec_evidence(
                source_record_id="live-current",
                source_period_start="2025-03-30",
                source_period_end="2025-06-28",
                canonical_period_end="2025-06-28",
                fiscal_quarter=2,
            ),
            _sec_evidence(
                source_record_id="live-comparable",
                source_period_start="2024-01-01",
                source_period_end="2024-03-30",
                canonical_period_end="2024-03-30",
                fiscal_year=2024,
            ),
        ]
        persisted_lineage["latest_eps_yoy_pct"] = [
            _sec_evidence(source_record_id="persisted-current"),
            _sec_evidence(
                source_record_id="persisted-comparable",
                source_period_start="2024-04-01",
                source_period_end="2024-06-29",
                canonical_period_end="2024-06-29",
                fiscal_year=2024,
                fiscal_quarter=2,
            ),
        ]
        result = self._diagnose(
            legacy=_snapshot(lineage=legacy_lineage),
            persisted=_snapshot(lineage=persisted_lineage),
        )

        per_input = result["classification"]["per_input"]["latest_eps_yoy_pct"]
        self.assertEqual(per_input["category"], "NOT_COMPARABLE")
        self.assertNotIn("NEWER_LIVE_EVIDENCE", per_input["reason_codes"])

    def test_changed_chain_member_without_temporal_direction_blocks_expected_drift(self):
        legacy_lineage = _lineage()
        persisted_lineage = _lineage()
        legacy_lineage["latest_eps_yoy_pct"] = [
            _sec_evidence(
                source_record_id="live-current",
                source_period_start="2025-03-30",
                source_period_end="2025-06-28",
                canonical_period_end="2025-06-28",
                fiscal_quarter=2,
            ),
            _sec_evidence(source_record_id="live-prior"),
        ]
        persisted_lineage["latest_eps_yoy_pct"] = [
            _sec_evidence(source_record_id="persisted-current"),
            _sec_evidence(source_record_id="persisted-prior"),
        ]

        result = self._diagnose(
            legacy=_snapshot(lineage=legacy_lineage),
            persisted=_snapshot(lineage=persisted_lineage),
        )

        per_input = result["classification"]["per_input"]["latest_eps_yoy_pct"]
        self.assertEqual(per_input["category"], "NOT_COMPARABLE")
        self.assertNotIn("NEWER_LIVE_EVIDENCE", per_input["reason_codes"])

    def test_same_yahoo_logical_slot_with_different_capture_is_capture_time_difference(self):
        legacy = _yahoo_evidence(observed_at="2025-05-06T12:00:00+00:00", raw_id=301)
        persisted = _yahoo_evidence(observed_at="2025-05-03T12:00:00+00:00", raw_id=201)
        result = self._diagnose(
            legacy=_snapshot(lineage=_lineage(legacy)),
            persisted=_snapshot(lineage=_lineage(persisted)),
        )

        alignment = result["evidence_alignment"]["latest_eps"]
        self.assertEqual(alignment["relation"], "SAME_LOGICAL_SLOT")
        self.assertEqual(result["classification"]["primary"], "CAPTURE_TIME_DIFFERENCE")
        self.assertIn("PROVIDER_REVISION_POSSIBLE", result["classification"]["reason_codes"])

    def test_missing_observed_at_does_not_prove_capture_time_difference(self):
        legacy = _yahoo_evidence(observed_at=None)
        persisted = _yahoo_evidence(observed_at="2025-05-03T12:00:00+00:00")

        result = self._diagnose(
            legacy=_snapshot(lineage=_lineage(legacy)),
            persisted=_snapshot(lineage=_lineage(persisted)),
        )

        self.assertEqual(result["classification"]["primary"], "NOT_COMPARABLE")
        self.assertNotIn(
            "CAPTURE_TIME_DIFFERENCE",
            result["classification"]["categories_present"],
        )

    def test_missing_monetary_currency_makes_identity_ambiguous(self):
        evidence = _yahoo_evidence(currency=None)

        comparison = compare_evidence_lineage([evidence], [evidence])

        self.assertEqual(comparison["relation"], "AMBIGUOUS")

    def test_distinct_immutable_sec_revisions_are_not_a_capture_time_difference(self):
        legacy = _sec_evidence(
            source_record_id="immutable-revision-a",
            observed_at="2025-05-06T12:00:00+00:00",
            raw_id=301,
        )
        persisted = _sec_evidence(
            source_record_id="immutable-revision-b",
            observed_at="2025-05-03T12:00:00+00:00",
            raw_id=201,
        )
        result = self._diagnose(
            legacy=_snapshot(lineage=_lineage(legacy)),
            persisted=_snapshot(lineage=_lineage(persisted)),
        )

        self.assertEqual(result["classification"]["primary"], "NOT_COMPARABLE")
        self.assertNotIn("CAPTURE_TIME_DIFFERENCE", result["classification"]["categories_present"])

    def test_different_source_variant_for_same_purpose_is_source_drift(self):
        legacy = _yahoo_evidence()
        persisted = _yahoo_evidence(
            source_variant="yfinance.quarterly_income_stmt",
            source_metric_name="Diluted EPS",
            provider_id="Diluted EPS:2025-03-29",
        )
        result = self._diagnose(
            legacy=_snapshot(lineage=_lineage(legacy)),
            persisted=_snapshot(lineage=_lineage(persisted)),
        )

        self.assertEqual(result["classification"]["primary"], "SOURCE_DRIFT")
        self.assertIn("DIFFERENT_SOURCE_VARIANT", result["classification"]["reason_codes"])

    def test_ambiguous_identity_is_not_comparable(self):
        ambiguous = _sec_evidence(
            source_record_id=None,
            fiscal_year=None,
            canonical_period_end=None,
        )
        result = self._diagnose(legacy=_snapshot(lineage=_lineage(ambiguous)))

        self.assertEqual(result["classification"]["primary"], "NOT_COMPARABLE")
        self.assertIn("IDENTITY_AMBIGUOUS", result["classification"]["reason_codes"])

    def test_missing_snapshots_are_not_comparable(self):
        missing_legacy = diagnose_c_snapshots(
            None, _snapshot(), capture_metadata=CAPTURE_METADATA,
        )
        missing_persisted = diagnose_c_snapshots(
            _snapshot(), None, capture_metadata=CAPTURE_METADATA,
        )

        self.assertEqual(missing_legacy["classification"]["primary"], "NOT_COMPARABLE")
        self.assertIn("MISSING_LEGACY", missing_legacy["classification"]["reason_codes"])
        self.assertEqual(missing_legacy["acquisition_status"]["run_status"], "FAILED")
        self.assertEqual(missing_persisted["classification"]["primary"], "NOT_COMPARABLE")
        self.assertIn("MISSING_PERSISTED", missing_persisted["classification"]["reason_codes"])

    def test_missing_persisted_input_exposes_unknown_ingestion_completeness(self):
        persisted_report = _report()
        del persisted_report["latest_eps"]

        result = self._diagnose(persisted=_snapshot(report=persisted_report))

        reasons = result["classification"]["per_input"]["latest_eps"]["reason_codes"]
        self.assertIn("MISSING_PERSISTED", reasons)
        self.assertIn("INGESTION_COMPLETENESS_NOT_ESTABLISHED", reasons)

    def test_provider_failure_is_partial_not_regression(self):
        legacy = _snapshot(
            acquisition_status="PARTIAL",
            provider_failures=[{
                "provider": "SEC",
                "reason": "timeout",
                "affects_comparability": True,
            }],
        )
        result = self._diagnose(legacy=legacy)

        self.assertEqual(result["classification"]["primary"], "NOT_COMPARABLE")
        self.assertIn("PROVIDER_FAILURE", result["classification"]["reason_codes"])
        self.assertEqual(result["acquisition_status"]["run_status"], "PARTIAL")

    def test_provider_failure_without_explicit_impact_cannot_become_regression(self):
        persisted_report = _report()
        persisted_report["latest_eps_yoy_pct"] += 1.0
        legacy = _snapshot(
            acquisition_status="PARTIAL",
            provider_failures=[{"provider": "SEC", "reason": "timeout"}],
        )

        result = self._diagnose(
            legacy=legacy,
            persisted=_snapshot(report=persisted_report),
        )

        self.assertEqual(result["classification"]["primary"], "NOT_COMPARABLE")
        self.assertNotEqual(result["classification"]["primary"], "POSSIBLE_REGRESSION")

    def test_missing_acquisition_status_and_one_missing_report_are_not_complete(self):
        legacy = _snapshot()
        del legacy["acquisition_status"]
        persisted_without_report = _snapshot()
        persisted_without_report["fundamental_report"] = None
        missing_status = self._diagnose(legacy=legacy)
        missing_report = self._diagnose(
            persisted=persisted_without_report,
        )

        self.assertEqual(missing_status["acquisition_status"]["run_status"], "PARTIAL")
        self.assertNotEqual(missing_status["acquisition_status"]["legacy_status"], "COMPLETE")
        self.assertIn(
            "INGESTION_COMPLETENESS_NOT_ESTABLISHED",
            missing_status["classification"]["reason_codes"],
        )
        self.assertEqual(missing_report["acquisition_status"]["run_status"], "PARTIAL")


class DerivedIdentityTests(unittest.TestCase):
    def test_sec_identity_type_is_certification_not_physical_key(self):
        legacy = _sec_evidence(source_identity_type=None)
        persisted = _sec_evidence(source_identity_type="SEC_ACCESSION")
        result = compare_evidence_lineage([legacy], [persisted])
        self.assertEqual(result["relation"], "SAME_EVIDENCE")

    def test_conflicting_sec_identity_type_fails_closed(self):
        legacy = _sec_evidence(source_identity_type="OTHER_TYPE")
        persisted = _sec_evidence(source_identity_type="SEC_ACCESSION")
        result = compare_evidence_lineage([legacy], [persisted])
        self.assertEqual(result["relation"], "AMBIGUOUS")

    def test_sec_identity_canonicalizes_date_representations(self):
        legacy = _sec_evidence(
            source_period_start=date(2024, 12, 29),
            source_period_end=date(2025, 3, 29),
        )
        persisted = _sec_evidence(
            source_period_start="2024-12-29",
            source_period_end="2025-03-29",
        )
        result = compare_evidence_lineage([legacy], [persisted])
        self.assertEqual(result["relation"], "SAME_EVIDENCE")

    def test_eps_lineage_uses_exact_last_four_score_observations(self):
        legacy_report = {"eps_yoy_pct": [{"date": f"202{i}-01-01", "value": float(i)} for i in range(12)]}
        persisted_report = {"eps_yoy_pct": [{"date": f"202{i}-01-01", "value": float(i)} for i in range(15)]}
        def lineage(ids):
            return [
                _sec_evidence(source_record_id=f"acc-{index}")
                for item in ids
                for index in (item, item)
            ]
        result = _compare_input_lineage(
            "eps_yoy_pct", legacy_report, persisted_report,
            lineage(range(12)), lineage(list(range(-3, 0)) + list(range(12))),
        )
        self.assertEqual(result["relation"], "SAME_EVIDENCE")
        self.assertEqual(result["full_history_relation"], "DIFFERENT")
        self.assertEqual(result["score_relevant_relation"], "SAME_EVIDENCE")

    def test_eps_score_relevant_identity_difference_is_not_hidden(self):
        report = {"eps_yoy_pct": [{"date": f"202{i}-01-01", "value": float(i)} for i in range(12)]}
        def lineage(ids):
            return [
                _sec_evidence(source_record_id=f"acc-{index}")
                for item in ids
                for index in (item, item)
            ]
        changed = list(range(12))
        changed[-1] = 99
        result = _compare_input_lineage(
            "eps_yoy_pct", report, report,
            lineage(range(12)), lineage(changed),
        )
        self.assertEqual(result["score_relevant_relation"], "DIFFERENT_EVIDENCE")

    def test_same_strong_identity_remains_same_evidence_when_processing_versions_differ(self):
        persisted = _sec_evidence(normalizer_version="sec-normalized-v2")
        legacy = _sec_evidence(normalizer_version=None)
        legacy.pop("normalizer_version")
        legacy["origin_normalizer_version"] = None
        result = compare_evidence_lineage([legacy], [persisted])
        self.assertEqual(result["relation"], "SAME_EVIDENCE")
        self.assertEqual(result["processing_version_status"], "LEGACY_VERSION_UNKNOWN")

    def test_different_sec_accession_is_different_evidence_even_same_period(self):
        legacy = _sec_evidence(source_record_id="0000320193-25-000057")
        persisted = _sec_evidence(source_record_id="0000320193-25-000058")
        result = compare_evidence_lineage([legacy], [persisted])
        self.assertEqual(result["relation"], "DIFFERENT_EVIDENCE")

    def test_same_yoy_number_with_different_comparable_is_not_same_evidence(self):
        legacy = [_sec_evidence(raw_id=1), _sec_evidence(raw_id=2, source_record_id="prior-a")]
        persisted = [_sec_evidence(raw_id=1), _sec_evidence(raw_id=3, source_record_id="prior-b")]

        result = compare_evidence_lineage(legacy, persisted)

        self.assertNotEqual(result["relation"], "SAME_EVIDENCE")

    def test_same_acceleration_number_with_different_yoy_chain_is_not_same_evidence(self):
        first = _sec_evidence(raw_id=1)
        second = _sec_evidence(raw_id=2, source_record_id="second")
        third = _sec_evidence(raw_id=3, source_record_id="third")

        changed = compare_evidence_lineage([first, second], [first, third])
        reordered = compare_evidence_lineage([first, second], [second, first])

        self.assertNotEqual(changed["relation"], "SAME_EVIDENCE")
        self.assertNotEqual(reordered["relation"], "SAME_EVIDENCE")
        self.assertTrue(reordered["order_changed"])

    def test_incomplete_derived_lineage_cannot_support_possible_regression(self):
        persisted_report = _report()
        persisted_report["latest_eps_yoy_pct"] += 1.0
        legacy_lineage = _lineage()
        persisted_lineage = _lineage()
        legacy_lineage["latest_eps_yoy_pct"] = [_sec_evidence()]
        persisted_lineage["latest_eps_yoy_pct"] = [_sec_evidence()]

        result = diagnose_c_snapshots(
            _snapshot(lineage=legacy_lineage),
            _snapshot(report=persisted_report, lineage=persisted_lineage),
            capture_metadata=CAPTURE_METADATA,
        )

        alignment = result["evidence_alignment"]["latest_eps_yoy_pct"]
        self.assertEqual(alignment["relation"], "AMBIGUOUS")
        self.assertIn("INCOMPLETE_DERIVED_LINEAGE", alignment["reason_codes"])
        self.assertEqual(
            result["classification"]["per_input"]["latest_eps_yoy_pct"]["category"],
            "NOT_COMPARABLE",
        )


class FreshnessTests(unittest.TestCase):
    def test_fixed_timestamps_produce_snapshot_span_ages_and_capture_delta(self):
        first = _sec_evidence(
            observed_at="2025-05-01T12:00:00+00:00",
            source_available_at="2025-05-01T10:00:00+00:00",
        )
        second = _sec_evidence(
            observed_at="2025-05-03T12:00:00+00:00",
            source_available_at="2025-05-03T08:00:00+00:00",
            raw_id=102,
            source_record_id="second",
        )

        result = calculate_freshness(
            {"eps_yoy_pct": [first, second]}, CAPTURE_METADATA,
        )

        self.assertEqual(result["persisted_snapshot_time"], "2025-05-03T12:00:00+00:00")
        self.assertEqual(result["persisted_snapshot_span"], {
            "start": "2025-05-01T12:00:00+00:00",
            "end": "2025-05-03T12:00:00+00:00",
        })
        self.assertEqual(result["evidence"][0]["ingestion_age_seconds"], 345600.0)
        self.assertEqual(result["evidence"][0]["publication_age_seconds"], 352920.0)
        self.assertEqual(result["capture_delta_seconds"], 120.0)
        self.assertEqual(result["evidence"][0]["availability_precision"], "TIMESTAMP_PRECISION")

    def test_sec_filed_date_is_date_precision_and_yahoo_unknown_stays_unknown(self):
        sec = _sec_evidence(source_available_at=None, filed_date="2025-05-02")
        yahoo = _yahoo_evidence(source_available_at=None, filed_date=None)

        result = calculate_freshness(
            {"sec": [sec], "yahoo": [yahoo]}, CAPTURE_METADATA,
        )

        by_source = {item["source"]: item for item in result["evidence"]}
        self.assertEqual(by_source["SEC"]["evidence_available_at"], "2025-05-02")
        self.assertEqual(by_source["SEC"]["availability_precision"], "DATE_PRECISION")
        self.assertIsNone(by_source["YAHOO"]["evidence_available_at"])
        self.assertEqual(by_source["YAHOO"]["availability_precision"], "UNKNOWN")


class SemanticGapAndScoreTests(unittest.TestCase):
    def _gate_inputs(self):
        contract_fields = (
            "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
            "latest_revenue_yoy_pct", "previous_revenue_yoy_pct",
            "revenue_acceleration_pp", "latest_eps", "eps_yoy_pct",
            "eps_loss_to_profit", "data_integrity", "split_integrity_status",
        )
        values = {field: 1 for field in contract_fields}
        values.update({
            "data_integrity": "VERIFIED",
            "split_integrity_status": "NO_RECENT_SPLITS",
        })
        report = {
            **values,
            "independently_reconstructed_by_new_architecture": True,
            "c_input_contract": {
                "inputs": {
                    field: {
                        "value": values[field],
                        "independently_reconstructed": True,
                    }
                    for field in contract_fields
                },
            },
        }
        classification = {
            "per_input": {
                field: {"category": "NUMERIC_EQUIVALENT"}
                for field in FUNDAMENTAL_INPUT_KEYS
            },
        }
        classification["per_input"].update({
            "data_integrity": {
                "category": "MATCH", "legacy_value_valid": True,
                "persisted_value_valid": True,
            },
            "split_integrity_status": {
                "category": "MATCH", "legacy_value_valid": True,
                "persisted_value_valid": True,
            },
        })
        alignments = {
            field: {"relation": "SAME_EVIDENCE"}
            for field in FUNDAMENTAL_INPUT_KEYS
        }
        score = {
            "equivalent": True,
            "independent_inputs": True,
            "shared_complements": None,
        }
        acquisition = {"provider_failures": []}
        return report, classification, alignments, score, acquisition

    def _run_gate(self, values, semantic_gaps=()):
        return _final_end_to_end_gate(
            values[0], values[1], values[2], values[3], semantic_gaps, values[4],
        )

    def test_final_gate_accepts_score_relevant_contract_with_full_history_difference(self):
        report, classification, alignments, score, acquisition = self._gate_inputs()
        alignments["eps_yoy_pct"].update({
            "full_history_relation": "DIFFERENT",
            "score_relevant_relation": "SAME_EVIDENCE",
        })
        result = _final_end_to_end_gate(
            report, classification, alignments, score, [], acquisition,
        )
        self.assertTrue(result["equivalent"])
        self.assertEqual(result["status"], "ESTABLISHED")
        self.assertEqual(result["input_contract_validation"]["required_count"], 11)
        self.assertEqual(result["input_contract_validation"]["valid_count"], 11)
        self.assertEqual(result["input_contract_validation"]["independent_count"], 11)
        self.assertEqual(result["input_contract_validation"]["equivalent_count"], 11)

    def test_final_gate_rejects_last_four_value_difference(self):
        values = self._gate_inputs()
        values[1]["per_input"]["eps_yoy_pct"] = {"category": "POSSIBLE_REGRESSION"}
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])

    def test_final_gate_rejects_score_relevant_identity_difference(self):
        values = self._gate_inputs()
        values[2]["eps_yoy_pct"] = {"relation": "DIFFERENT_EVIDENCE"}
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])

    def test_final_gate_rejects_semantic_gap(self):
        values = self._gate_inputs()
        result = self._run_gate(
            values,
            [{"code": "DATA_INTEGRITY_NOT_RECONSTRUCTED", "score_relevant": True}],
        )
        self.assertFalse(result["equivalent"])

    def test_final_gate_rejects_shared_complements(self):
        values = self._gate_inputs()
        values[3]["shared_complements"] = {"data_integrity": "VERIFIED"}
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])

    def test_final_gate_rejects_score_difference(self):
        values = self._gate_inputs()
        values[3]["equivalent"] = False
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])

    def test_non_comparable_provider_failure_does_not_block_gate(self):
        values = self._gate_inputs()
        values[4]["provider_failures"] = [{"affects_comparability": False}]
        result = self._run_gate(values)
        self.assertTrue(result["equivalent"])

    def test_unknown_ingestion_completeness_blocks_without_provider_failure(self):
        values = self._gate_inputs()
        values[1]["reason_codes"] = ["INGESTION_COMPLETENESS_NOT_ESTABLISHED"]
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])
        self.assertEqual(result["status"], "NOT_ESTABLISHED")

    def test_unknown_ingestion_completeness_blocks_with_non_comparable_failure(self):
        values = self._gate_inputs()
        values[1]["reason_codes"] = ["INGESTION_COMPLETENESS_NOT_ESTABLISHED"]
        values[4]["provider_failures"] = [{"affects_comparability": False}]
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])
        self.assertFalse(result["conditions"]["ingestion_completeness_established"])

    def test_blocking_provider_failure_remains_blocking(self):
        values = self._gate_inputs()
        values[4]["provider_failures"] = [{"affects_comparability": True}]
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])
        self.assertFalse(result["conditions"]["provider_failures_non_blocking"])

    def test_final_gate_rejects_non_mapping_required_contract_entries(self):
        malformed_values = (None, [], "", object())
        for malformed in malformed_values:
            with self.subTest(malformed=type(malformed).__name__):
                values = self._gate_inputs()
                values[0]["c_input_contract"]["inputs"]["data_integrity"] = malformed
                result = self._run_gate(values)
                self.assertFalse(result["equivalent"])
                self.assertEqual(result["status"], "NOT_ESTABLISHED")
                self.assertFalse(result["conditions"]["all_eleven_inputs_independent"])
                self.assertIn("C_INPUT_CONTRACT_ENTRY_INVALID", result["reason_codes"])

    def test_final_gate_rejects_all_eleven_none_entries(self):
        values = self._gate_inputs()
        values[0]["c_input_contract"]["inputs"] = {
            key: None for key in values[0]["c_input_contract"]["inputs"]
        }
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])
        self.assertEqual(result["input_contract_validation"]["valid_count"], 0)
        self.assertEqual(result["input_contract_validation"]["independent_count"], 0)

    def test_final_gate_rejects_missing_or_empty_contract(self):
        for mutation in ("missing_key", "empty_contract"):
            with self.subTest(mutation=mutation):
                values = self._gate_inputs()
                if mutation == "missing_key":
                    del values[0]["c_input_contract"]["inputs"]["latest_eps"]
                else:
                    values[0]["c_input_contract"]["inputs"] = {}
                result = self._run_gate(values)
                self.assertFalse(result["equivalent"])
                self.assertIn("C_INPUT_CONTRACT_COUNT_INVALID", result["reason_codes"])

    def test_final_gate_rejects_missing_null_or_false_independence(self):
        for state in ("missing", None, False):
            with self.subTest(state=state):
                values = self._gate_inputs()
                entry = values[0]["c_input_contract"]["inputs"]["latest_eps"]
                if state == "missing":
                    del entry["independently_reconstructed"]
                else:
                    entry["independently_reconstructed"] = state
                result = self._run_gate(values)
                self.assertFalse(result["equivalent"])
                self.assertIn("C_INPUT_NOT_INDEPENDENT", result["reason_codes"])

    def test_final_gate_rejects_data_integrity_difference_despite_equal_score(self):
        values = self._gate_inputs()
        values[1]["per_input"]["data_integrity"] = {"category": "POSSIBLE_REGRESSION"}
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])
        self.assertTrue(values[3]["equivalent"])
        self.assertFalse(result["conditions"]["integrity_semantics_equivalent"])

    def test_final_gate_rejects_split_integrity_difference_despite_equal_score(self):
        values = self._gate_inputs()
        values[1]["per_input"]["split_integrity_status"] = {"category": "POSSIBLE_REGRESSION"}
        result = self._run_gate(values)
        self.assertFalse(result["equivalent"])
        self.assertTrue(values[3]["equivalent"])
        self.assertFalse(result["conditions"]["integrity_semantics_equivalent"])

    def test_final_gate_rejects_invalid_data_integrity_even_when_both_sides_match(self):
        for invalid in (None, "", "BOGUS", [], 1, True, object()):
            with self.subTest(invalid=repr(invalid)):
                values = self._gate_inputs()
                values[0]["data_integrity"] = invalid
                values[0]["c_input_contract"]["inputs"]["data_integrity"]["value"] = invalid
                values[1]["per_input"]["data_integrity"] = {"category": "MATCH"}
                result = self._run_gate(values)
                self.assertFalse(result["equivalent"])
                self.assertIn("INTEGRITY_STATUS_INVALID", result["reason_codes"])

    def test_final_gate_rejects_invalid_split_status_even_when_both_sides_match(self):
        for invalid in (None, "", "BOGUS", [], 1, False, object()):
            with self.subTest(invalid=repr(invalid)):
                values = self._gate_inputs()
                values[0]["split_integrity_status"] = invalid
                values[0]["c_input_contract"]["inputs"]["split_integrity_status"]["value"] = invalid
                values[1]["per_input"]["split_integrity_status"] = {"category": "MATCH"}
                result = self._run_gate(values)
                self.assertFalse(result["equivalent"])
                self.assertIn("INTEGRITY_STATUS_INVALID", result["reason_codes"])

    def test_full_diagnose_rejects_matching_invalid_integrity_status(self):
        persisted = _report()
        persisted.update({
            "data_integrity": None,
            "split_integrity_status": "NO_RECENT_SPLITS",
            "independently_reconstructed_by_new_architecture": True,
        })
        persisted["c_input_contract"] = {
            "inputs": {
                key: {"value": persisted[key], "independently_reconstructed": True}
                for key in (
                    "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
                    "latest_revenue_yoy_pct", "previous_revenue_yoy_pct",
                    "revenue_acceleration_pp", "latest_eps", "eps_yoy_pct",
                    "eps_loss_to_profit", "data_integrity", "split_integrity_status",
                )
            },
        }
        legacy = copy.deepcopy(persisted)
        legacy.pop("independently_reconstructed_by_new_architecture")
        legacy.pop("c_input_contract")
        result = diagnose_c_snapshots(
            _snapshot(report=legacy),
            _snapshot(report=persisted),
            capture_metadata=CAPTURE_METADATA,
        )
        self.assertFalse(result["end_to_end_equivalent"])
        self.assertEqual(result["end_to_end_status"], "NOT_ESTABLISHED")

    def test_diagnose_compares_integrity_values_directly(self):
        persisted = self._gate_inputs()[0]
        legacy = copy.deepcopy(persisted)
        legacy.pop("independently_reconstructed_by_new_architecture")
        legacy.pop("c_input_contract")
        for field, legacy_value, persisted_value in (
            ("data_integrity", "REVIEW_REQUIRED", "VERIFIED"),
            ("split_integrity_status", "UNKNOWN", "NO_RECENT_SPLITS"),
        ):
            with self.subTest(field=field):
                legacy_case = copy.deepcopy(legacy)
                legacy_case[field] = legacy_value
                persisted_case = copy.deepcopy(persisted)
                persisted_case[field] = persisted_value
                persisted_case["c_input_contract"]["inputs"][field]["value"] = persisted_value
                result = diagnose_c_snapshots(
                    _snapshot(report=legacy_case),
                    _snapshot(report=persisted_case),
                    capture_metadata=CAPTURE_METADATA,
                )
                self.assertFalse(result["end_to_end_equivalent"])
                self.assertEqual(
                    result["classification"]["per_input"][field]["category"],
                    "POSSIBLE_REGRESSION",
                )

    def test_split_and_data_integrity_gaps_override_match_with_explicit_severity(self):
        for code, score_relevant, severity in (
            ("CORPORATE_ACTIONS_NOT_RECONSTRUCTED", False, "INFO"),
            ("DATA_INTEGRITY_NOT_RECONSTRUCTED", True, "REVIEW_REQUIRED"),
        ):
            with self.subTest(code=code):
                result = diagnose_c_snapshots(
                    _snapshot(),
                    _snapshot(),
                    capture_metadata=CAPTURE_METADATA,
                    semantic_gaps=[{"code": code, "score_relevant": score_relevant}],
                )
                self.assertEqual(result["classification"]["primary"], "SEMANTIC_GAP")
                self.assertEqual(result["severity"], severity)
                self.assertFalse(result["end_to_end_equivalent"])
                self.assertEqual(result["end_to_end_status"], "NOT_ESTABLISHED")

    def test_gap_without_score_relevance_is_unknown_and_not_silently_informational(self):
        result = diagnose_c_snapshots(
            _snapshot(),
            _snapshot(),
            capture_metadata=CAPTURE_METADATA,
            semantic_gaps=[{"code": "CORPORATE_ACTIONS_NOT_RECONSTRUCTED"}],
        )

        self.assertIsNone(result["semantic_gaps"][0]["score_relevant"])
        self.assertEqual(result["severity"], "WARNING")

    def test_score_isolation_uses_shared_complements_without_mutating_end_to_end_score(self):
        legacy_score = {"marker": "untouched"}
        persisted_report = _report()
        persisted_report["latest_eps_yoy_pct"] += 10.0
        complements = {
            "data_integrity": "VERIFIED",
            "split_integrity_status": "NO_RECENT_SPLITS",
        }

        result = diagnose_c_snapshots(
            _snapshot(legacy_end_to_end_score=legacy_score),
            _snapshot(report=persisted_report),
            capture_metadata=CAPTURE_METADATA,
            shared_live_complements=complements,
        )

        self.assertEqual(result["legacy_end_to_end_score"], legacy_score)
        self.assertEqual(
            result["score_isolation"]["shared_complements"]["values"],
            complements,
        )
        self.assertFalse(result["score_isolation"]["equivalent"])
        self.assertFalse(result["end_to_end_equivalent"])


class DeterminismAndSafetyTests(unittest.TestCase):
    def test_repeated_diagnostic_is_byte_for_byte_json_deterministic(self):
        kwargs = {
            "capture_metadata": CAPTURE_METADATA,
            "shared_live_complements": {
                "data_integrity": "VERIFIED",
                "split_integrity_status": "NO_RECENT_SPLITS",
            },
        }
        first = diagnose_c_snapshots(_snapshot(), _snapshot(), **kwargs)
        second = diagnose_c_snapshots(_snapshot(), _snapshot(), **kwargs)

        first_json = json.dumps(first, sort_keys=True, separators=(",", ":"))
        second_json = json.dumps(second, sort_keys=True, separators=(",", ":"))
        self.assertEqual(first_json, second_json)

    def test_injected_datetime_metadata_is_normalized_for_json_output(self):
        metadata = {
            "persisted_read_at": datetime(2025, 5, 5, 12, tzinfo=timezone.utc),
            "legacy_capture_started_at": datetime(2025, 5, 5, 12, 1, tzinfo=timezone.utc),
            "legacy_capture_completed_at": datetime(2025, 5, 5, 12, 2, tzinfo=timezone.utc),
        }

        result = diagnose_c_snapshots(
            _snapshot(), _snapshot(), capture_metadata=metadata,
        )

        json.dumps(result, sort_keys=True)
        self.assertEqual(result["metadata"]["persisted_read_at"], "2025-05-05T12:00:00+00:00")

    def test_native_dates_in_lineage_remain_json_serializable(self):
        evidence = _sec_evidence(
            source_period_start=date(2024, 12, 29),
            source_period_end=date(2025, 3, 29),
            canonical_period_end=date(2025, 3, 29),
            filed_date=date(2025, 5, 2),
            observed_at=datetime(2025, 5, 3, 12, tzinfo=timezone.utc),
        )
        later_capture = copy.deepcopy(evidence)
        later_capture["observed_at"] = datetime(2025, 5, 4, 12, tzinfo=timezone.utc)

        result = diagnose_c_snapshots(
            _snapshot(lineage=_lineage(evidence)),
            _snapshot(lineage=_lineage(later_capture)),
            capture_metadata=CAPTURE_METADATA,
        )

        json.dumps(result, sort_keys=True)

    def test_import_does_not_require_network_database_or_legacy_dependencies(self):
        code = r'''
import builtins

blocked = {"requests", "yfinance", "psycopg", "database.db", "fundamental_c"}
original = builtins.__import__

def guarded(name, *args, **kwargs):
    if name in blocked or any(name.startswith(item + ".") for item in blocked):
        raise AssertionError("forbidden import: " + name)
    return original(name, *args, **kwargs)

builtins.__import__ = guarded
import database.c_live_diagnostic
print("IMPORT_SAFE")
'''
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "IMPORT_SAFE")


if __name__ == "__main__":
    unittest.main()
