"""Read-only orchestration for one persisted and one legacy C snapshot."""

from __future__ import annotations

import copy
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Mapping, Sequence


_SEMANTIC_GAPS = (
    {"code": "CORPORATE_ACTIONS_NOT_RECONSTRUCTED", "score_relevant": False},
    {"code": "DATA_INTEGRITY_NOT_RECONSTRUCTED", "score_relevant": True},
)

_EVIDENCE_FIELDS = (
    "company_id",
    "source",
    "source_variant",
    "metric",
    "source_metric_name",
    "fiscal_year",
    "fiscal_quarter",
    "canonical_period_end",
    "series_date",
    "source_period_start",
    "source_period_end",
    "filed_date",
    "source_available_at",
    "observed_at",
    "selected_observation_id",
    "raw_id",
    "unit",
    "currency",
    "source_unit",
    "source_scale_factor",
    "normalizer_version",
    "selection_policy_version",
    "selection_reason",
    "comparison_rules_version",
    "comparison_status",
    "comparison_reason",
    "comparison_reference_id",
    "comparison_difference_pct",
    "alignment_method",
    "alignment_days",
    "alignment_reference_id",
    "source_record_id",
    "immutable_revision_id",
    "provider_revision_id",
    "provider_id",
    "identity_is_immutable",
    "source_identity_type",
)


class AcquisitionFailure(RuntimeError):
    """Expected failure at an injected or external acquisition boundary."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _json_safe_decimals(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe_decimals(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_decimals(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_decimals(item) for item in value]
    return value


def _default_load_persisted_rows(company_id: int) -> list[dict]:
    from database.effective_fundamentals import load_effective_current

    try:
        return load_effective_current(company_id)
    except Exception as exc:
        missing_configuration = (
            isinstance(exc, RuntimeError)
            and str(exc).startswith(
                "Faltan variables de entorno para PostgreSQL:"
            )
        )
        invalid_port = False
        if isinstance(exc, ValueError):
            try:
                int(os.getenv("CANSLIM_DB_PORT", ""))
            except ValueError:
                invalid_port = True
        driver_failure = any(
            base.__module__.split(".")[0] == "psycopg"
            for base in type(exc).__mro__
        )
        if missing_configuration or invalid_port or driver_failure:
            raise AcquisitionFailure("persisted read failed") from None
        raise


def _default_acquire_legacy_snapshot(
    ticker: str, *, company_id: int, capture_clock: Callable,
) -> dict:
    from fundamental_c import _analyze_current_earnings_snapshot

    return _analyze_current_earnings_snapshot(
        ticker,
        company_id=company_id,
        capture_clock=capture_clock,
    )


def _default_diagnose(*args, **kwargs) -> dict:
    from database.c_live_diagnostic import diagnose_c_snapshots

    return diagnose_c_snapshots(*args, **kwargs)


def _adapter_dependencies():
    from database.c_fundamentals_adapter import (
        _effective_observations,
        _latest_acceleration,
        _latest_observation,
        build_c_fundamental_report,
    )
    from database.effective_fundamentals import (
        annual_comparisons_by_source,
        growth_acceleration_by_source,
        growth_yoy_by_source,
    )

    return (
        _effective_observations,
        _latest_acceleration,
        _latest_observation,
        build_c_fundamental_report,
        annual_comparisons_by_source,
        growth_acceleration_by_source,
        growth_yoy_by_source,
    )


def _failure(provider: str, operation: str, reason_code: str) -> dict:
    return {
        "provider": provider,
        "operation": operation,
        "reason": "acquisition operation failed",
        "reason_code": reason_code,
        "affects_comparability": True,
    }


def _evidence(row: Mapping) -> dict:
    """Project only metadata that is explicitly present on the effective row."""

    evidence = {
        field: copy.deepcopy(row[field])
        for field in _EVIDENCE_FIELDS
        if field in row
    }
    if "source_record_id" not in evidence:
        explicit_accession = row.get("accession") or row.get("accn")
        if explicit_accession not in (None, ""):
            evidence["source_record_id"] = copy.deepcopy(explicit_accession)
    return evidence


def _identity_assessment(rows: Sequence[Mapping] | None) -> dict:
    limitations = []
    rows = list(rows or [])
    sec_rows = [row for row in rows if row.get("source") == "SEC"]
    missing_sec_accessions = sum(
        not (
            (
                row.get("source_record_id") not in (None, "")
                and row.get("source_identity_type") in (None, "SEC_ACCESSION")
            )
            or row.get("accession") not in (None, "")
            or row.get("accn") not in (None, "")
        )
        for row in sec_rows
    )
    if missing_sec_accessions:
        limitations.append("PERSISTED_SEC_ACCESSION_NOT_EXPOSED")
    return {
        "origin_metadata_preserved": True,
        "raw_id_used_as_accession": False,
        "sec_rows_total": len(sec_rows),
        "sec_rows_without_accession": missing_sec_accessions,
        "limitations": limitations,
    }


def _build_persisted_lineage(rows: Sequence[Mapping]) -> dict[str, list[dict]]:
    (
        effective_observations,
        latest_acceleration,
        latest_observation,
        _build_report,
        annual_comparisons_by_source,
        growth_acceleration_by_source,
        growth_yoy_by_source,
    ) = _adapter_dependencies()

    observations = effective_observations(rows)
    growth = growth_yoy_by_source(observations)
    accelerations = growth_acceleration_by_source(growth)
    by_id = {
        row["selected_observation_id"]: _evidence(row)
        for row in rows
    }

    def observation_evidence(item) -> dict:
        return copy.deepcopy(by_id[item.observation.id])

    def growth_pair(item) -> list[dict]:
        if item is None:
            return []
        return [
            observation_evidence(item.current),
            observation_evidence(item.comparable),
        ]

    eps_acceleration = latest_acceleration(accelerations, "EPS_DILUTED")
    revenue_acceleration = latest_acceleration(accelerations, "REVENUE")
    latest_eps_observation = latest_observation(observations, "EPS_DILUTED")
    eps_growth = sorted(
        (item for item in growth if item.metric == "EPS_DILUTED"),
        key=lambda item: (
            item.current.observation.series_date,
            item.current.observation.source_variant,
            item.current.observation.raw_id,
        ),
    )
    eps_comparisons = annual_comparisons_by_source(observations, "EPS_DILUTED")
    latest_eps_comparison = next(
        (
            item for item in eps_comparisons
            if latest_eps_observation is not None
            and item.current is latest_eps_observation
        ),
        None,
    )

    latest_eps_pair = growth_pair(
        None if eps_acceleration is None else eps_acceleration.latest_yoy
    )
    previous_eps_pair = growth_pair(
        None if eps_acceleration is None else eps_acceleration.previous_yoy
    )
    latest_revenue_pair = growth_pair(
        None if revenue_acceleration is None else revenue_acceleration.latest_yoy
    )
    previous_revenue_pair = growth_pair(
        None if revenue_acceleration is None else revenue_acceleration.previous_yoy
    )
    return {
        "latest_eps_yoy_pct": latest_eps_pair,
        "previous_eps_yoy_pct": previous_eps_pair,
        "eps_acceleration_pp": latest_eps_pair + previous_eps_pair,
        "latest_revenue_yoy_pct": latest_revenue_pair,
        "previous_revenue_yoy_pct": previous_revenue_pair,
        "revenue_acceleration_pp": latest_revenue_pair + previous_revenue_pair,
        "latest_eps": (
            []
            if latest_eps_observation is None
            else [observation_evidence(latest_eps_observation)]
        ),
        "eps_yoy_pct": [
            evidence
            for item in eps_growth
            for evidence in growth_pair(item)
        ],
        "eps_loss_to_profit": (
            []
            if latest_eps_comparison is None
            else [
                observation_evidence(latest_eps_comparison.current),
                observation_evidence(latest_eps_comparison.comparable),
            ]
        ),
    }


def _complete_report(report: Mapping) -> bool:
    scalar_fields = (
        "latest_eps_yoy_pct",
        "previous_eps_yoy_pct",
        "eps_acceleration_pp",
        "latest_revenue_yoy_pct",
        "previous_revenue_yoy_pct",
        "revenue_acceleration_pp",
        "latest_eps",
    )
    return (
        all(report.get(field) is not None for field in scalar_fields)
        and isinstance(report.get("eps_yoy_pct"), list)
        and bool(report["eps_yoy_pct"])
        and isinstance(report.get("eps_loss_to_profit"), bool)
    )


def _persisted_snapshot(rows: Sequence[Mapping]) -> dict:
    *_, build_report, __, ___, ____ = _adapter_dependencies()
    report = build_report(rows)
    complete = _complete_report(report)
    failures = [] if complete else [
        _failure("POSTGRESQL", "reconstruct_c_inputs", "PERSISTED_INPUTS_INCOMPLETE")
    ]
    return {
        "fundamental_report": report,
        "lineage_by_input": _build_persisted_lineage(rows),
        "provider_failures": failures,
        "acquisition_status": "COMPLETE" if complete else "PARTIAL",
        "semantic_gaps": copy.deepcopy(list(_SEMANTIC_GAPS)),
    }


def _failed_snapshot(provider: str, operation: str, reason_code: str) -> dict:
    return {
        "fundamental_report": None,
        "lineage_by_input": {},
        "provider_failures": [_failure(provider, operation, reason_code)],
        "acquisition_status": "FAILED",
        "semantic_gaps": copy.deepcopy(list(_SEMANTIC_GAPS)),
    }


def _append_acquisition_reason_codes(result: dict) -> None:
    classification = result.get("classification")
    acquisition = result.get("acquisition_status")
    if not isinstance(classification, dict) or not isinstance(acquisition, dict):
        return
    reasons = classification.setdefault("reason_codes", [])
    for failure in acquisition.get("provider_failures", []):
        reason_code = failure.get("reason_code")
        if reason_code and reason_code not in reasons:
            reasons.append(reason_code)


def run_live_c_diagnostic(
    ticker: str,
    company_id: int,
    *,
    acquire_legacy_snapshot=None,
    load_persisted_rows=None,
    clock=None,
    diagnose_snapshots=None,
) -> dict:
    """Run one read-only comparison from exactly two in-memory captures."""

    clock = clock or _utcnow
    load_persisted_rows = load_persisted_rows or _default_load_persisted_rows
    acquire_legacy_snapshot = (
        acquire_legacy_snapshot or _default_acquire_legacy_snapshot
    )
    diagnose_snapshots = diagnose_snapshots or _default_diagnose

    diagnostic_started_at = _iso(clock())
    try:
        rows = list(load_persisted_rows(company_id))
    except AcquisitionFailure:
        rows = None
        persisted = _failed_snapshot(
            "POSTGRESQL", "load_effective_current", "DB_READ_FAILED",
        )
    persisted_read_at = _iso(clock())
    if rows is not None:
        persisted = (
            _persisted_snapshot(rows)
            if rows
            else _failed_snapshot(
                "POSTGRESQL", "load_effective_current", "NO_EFFECTIVE_ROWS",
            )
        )

    try:
        legacy = acquire_legacy_snapshot(
            ticker,
            company_id=company_id,
            capture_clock=clock,
        )
    except AcquisitionFailure:
        legacy = _failed_snapshot(
            "LEGACY_LIVE", "acquire_legacy_snapshot", "LEGACY_ACQUISITION_FAILED",
        )

    capture_metadata = {
        "ticker": ticker,
        "company_id": company_id,
        "diagnostic_started_at": diagnostic_started_at,
        "persisted_read_at": persisted_read_at,
    }
    if isinstance(legacy, Mapping):
        legacy_metadata = legacy.get("capture_metadata", {})
        for key in (
            "legacy_capture_started_at",
            "legacy_capture_completed_at",
        ):
            if key in legacy_metadata:
                capture_metadata[key] = copy.deepcopy(legacy_metadata[key])
    shared_complements = (
        None
        if not isinstance(legacy, Mapping)
        else copy.deepcopy(legacy.get("shared_live_complements"))
    )
    result = diagnose_snapshots(
        legacy,
        persisted,
        capture_metadata=capture_metadata,
        shared_live_complements=shared_complements,
        semantic_gaps=copy.deepcopy(list(_SEMANTIC_GAPS)),
    )
    result["metadata"]["diagnostic_completed_at"] = _iso(clock())
    result["identity_assessment"] = _identity_assessment(rows)
    _append_acquisition_reason_codes(result)
    return _json_safe_decimals(result)
