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
    "source_identity_error",
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


def _default_load_persisted_rows(company_id: int, *, as_of: datetime | None = None):
    try:
        if as_of is None:
            from database.effective_fundamentals import load_effective_current
            rows = load_effective_current(company_id)
            return rows
        from database.c_data_integrity import (
            load_normalized_evidence, reconstruct_persisted_integrity,
        )
        from database.c_fundamentals_adapter import (
            _effective_observations,
            normalized_rows_to_effective_rows,
            build_c_fundamental_report_from_normalized,
        )
        from database.effective_fundamentals import annual_comparisons_by_source
        from database.corporate_actions import load_latest_capture_as_of

        normalized = load_normalized_evidence(company_id, as_of)
        visible_rows = list(normalized)
        capture, events = load_latest_capture_as_of(company_id, as_of)
        captures = [] if capture is None else [capture]
        events_by_capture_id = {} if capture is None else {capture.id: events}
        lineage_rows = normalized_rows_to_effective_rows(
            visible_rows, as_of=as_of,
        )
        effective_observations = _effective_observations(lineage_rows)
        fundamental_report = build_c_fundamental_report_from_normalized(
            visible_rows, as_of=as_of,
        )
        eps_observations = [
            item for item in effective_observations
            if item.observation.metric == "EPS_DILUTED"
        ]
        latest_eps = max(
            eps_observations,
            key=lambda item: item.observation.series_date,
            default=None,
        )
        latest_pair = next(
            (item for item in annual_comparisons_by_source(effective_observations, "EPS_DILUTED")
             if latest_eps is not None and item.current is latest_eps),
            None,
        )
        scalars = {
            key: fundamental_report[key]
            for key in (
                "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
                "latest_revenue_yoy_pct", "previous_revenue_yoy_pct",
                "revenue_acceleration_pp",
            )
        }
        scalars.update({
            "previous_eps_period": (
                None if latest_pair is None else latest_pair.comparable.observation.series_date
            ),
            "latest_eps_period": (
                None if latest_eps is None else latest_eps.observation.series_date
            ),
        })
        integrity = reconstruct_persisted_integrity(
            normalized, captures, events_by_capture_id,
            as_of=as_of, scalars=scalars,
        )
        return {
            "fundamental_rows": visible_rows,
            "lineage_rows": lineage_rows,
            "fundamental_report": fundamental_report,
            "integrity": integrity,
            "company_id": company_id,
            "as_of": as_of,
        }
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
    if any(row.get("source_identity_error") for row in sec_rows):
        limitations.append("PERSISTED_SEC_ACCESSION_INCONSISTENT")
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
    independent = isinstance(rows, Mapping) and "fundamental_rows" in rows
    payload = rows if independent else None
    effective_rows = (
        payload.get("lineage_rows", payload["fundamental_rows"])
        if independent else rows
    )
    report = (
        payload["fundamental_report"]
        if independent and "fundamental_report" in payload
        else build_report(effective_rows)
    )
    if independent:
        from database.c_fundamentals_adapter import build_independent_c_input_report
        integrity = payload.get("integrity")
        if integrity is None:
            raise ValueError("independent persisted payload lacks integrity reconstruction")
        if hasattr(integrity, "data_integrity"):
            integrity = {
                "data_integrity": integrity.data_integrity,
                "split_integrity_status": integrity.split_integrity_status,
            }
        report = build_independent_c_input_report(
            report, integrity,
            company_id=payload["company_id"],
            as_of=payload["as_of"],
            provenance=payload.get("provenance"),
        )
    complete = _complete_report(report)
    failures = [] if complete else [
        _failure("POSTGRESQL", "reconstruct_c_inputs", "PERSISTED_INPUTS_INCOMPLETE")
    ]
    integrity_summary = None
    if independent:
        source_integrity = payload.get("integrity")
        get_integrity = (
            (lambda name, default=None: source_integrity.get(name, default))
            if isinstance(source_integrity, Mapping)
            else (lambda name, default=None: getattr(source_integrity, name, default))
        )
        consistency_values = get_integrity("consistency", {}) or {}
        integrity_summary = {
            "data_integrity": get_integrity("data_integrity"),
            "data_quality": get_integrity("data_quality"),
            "split_integrity_status": get_integrity("split_integrity_status"),
            "current_yoy_crosses_split": get_integrity("current_yoy_crosses_split"),
            "shares_quality": get_integrity("shares_quality"),
            "warnings": list(get_integrity("warnings", ())),
            "diagnostics": list(get_integrity("diagnostics", ())),
            "consistency": {
                key: {
                    "status": value.get("status") if isinstance(value, Mapping) else value.status,
                    "max_diff_pct": value.get("max_diff_pct") if isinstance(value, Mapping) else value.max_diff_pct,
                    "avg_diff_pct": value.get("avg_diff_pct") if isinstance(value, Mapping) else value.avg_diff_pct,
                    "matched_count": value.get("matched_count") if isinstance(value, Mapping) else value.matched_count,
                    "reasons": list(value.get("reasons", ())) if isinstance(value, Mapping) else list(value.reasons),
                }
                for key, value in consistency_values.items()
            },
        }
    return {
        "fundamental_report": report,
        "lineage_by_input": _build_persisted_lineage(effective_rows),
        "provider_failures": failures,
        "acquisition_status": "COMPLETE" if complete else "PARTIAL",
        "semantic_gaps": [] if independent else copy.deepcopy(list(_SEMANTIC_GAPS)),
        "independently_reconstructed_by_new_architecture": independent,
        "integrity_reconstruction": integrity_summary,
    }


def _failed_snapshot(provider: str, operation: str, reason_code: str) -> dict:
    return {
        "fundamental_report": None,
        "lineage_by_input": {},
        "provider_failures": [_failure(provider, operation, reason_code)],
        "acquisition_status": "FAILED",
        "semantic_gaps": copy.deepcopy(list(_SEMANTIC_GAPS)),
    }


def load_independent_c_inputs(company_id: int, as_of: datetime) -> dict:
    """Read-only production boundary for the complete persisted C contract."""
    payload = _default_load_persisted_rows(company_id, as_of=as_of)
    if not isinstance(payload, Mapping) or "fundamental_rows" not in payload:
        raise AcquisitionFailure("independent persisted evidence unavailable")
    return _persisted_snapshot(payload)


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

    evaluation_as_of = clock()
    diagnostic_started_at = _iso(evaluation_as_of)
    try:
        if load_persisted_rows is _default_load_persisted_rows:
            loaded = load_persisted_rows(company_id, as_of=evaluation_as_of)
        else:
            loaded = load_persisted_rows(company_id)
        rows = loaded if isinstance(loaded, Mapping) else list(loaded)
    except AcquisitionFailure:
        rows = None
        persisted = _failed_snapshot(
            "POSTGRESQL", "load_persisted_evidence", "DB_READ_FAILED",
        )
    persisted_read_at = _iso(clock())
    if rows is not None:
        persisted = (
            _persisted_snapshot(rows)
            if rows
            else _failed_snapshot(
                "POSTGRESQL", "load_persisted_evidence", "NO_EFFECTIVE_ROWS",
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
    independent_persisted = bool(
        isinstance(persisted, Mapping)
        and persisted.get("independently_reconstructed_by_new_architecture") is True
    )
    shared_complements = (
        None
        if independent_persisted or not isinstance(legacy, Mapping)
        else copy.deepcopy(legacy.get("shared_live_complements"))
    )
    result = diagnose_snapshots(
        legacy,
        persisted,
        capture_metadata=capture_metadata,
        shared_live_complements=shared_complements,
        semantic_gaps=([] if independent_persisted else copy.deepcopy(list(_SEMANTIC_GAPS))),
    )
    result["metadata"]["diagnostic_completed_at"] = _iso(clock())
    identity_rows = (
        rows.get("fundamental_rows", ())
        if isinstance(rows, Mapping) else rows
    )
    result["identity_assessment"] = _identity_assessment(identity_rows)
    _append_acquisition_reason_codes(result)
    return _json_safe_decimals(result)
