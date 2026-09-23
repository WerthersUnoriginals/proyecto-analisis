"""Pure, deterministic classification for already-captured live C snapshots.

This module deliberately performs no acquisition, persistence, filesystem
access, or clock reads.  It layers evidence identity and temporal context on
top of the numeric and score comparators established by Task 10B.1.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, time, timezone
from typing import Iterable, Mapping, Sequence

from database.c_dual_run import (
    compare_fundamental_contract,
    compare_score_equivalence,
)
from database.c_dual_run_contract import FUNDAMENTAL_INPUT_KEYS


IDENTITY_STRENGTHS = ("STRONG", "LOGICAL", "AMBIGUOUS")
CLASSIFICATIONS = (
    "MATCH",
    "NUMERIC_EQUIVALENT",
    "EXPECTED_DRIFT",
    "SOURCE_DRIFT",
    "CAPTURE_TIME_DIFFERENCE",
    "POSSIBLE_REGRESSION",
    "SEMANTIC_GAP",
    "NOT_COMPARABLE",
)
SEVERITIES = ("INFO", "WARNING", "REVIEW_REQUIRED")
RUN_STATUSES = ("COMPLETE", "PARTIAL", "FAILED")

_SEMANTIC_GAP_CODES = {
    "CORPORATE_ACTIONS_NOT_RECONSTRUCTED",
    "DATA_INTEGRITY_NOT_RECONSTRUCTED",
}
_STRONG_BASE_FIELDS = (
    "company_id",
    "source",
    "source_variant",
    "metric",
    "source_metric_name",
    "unit",
    "source_period_start",
    "source_period_end",
)
_LOGICAL_FIELDS = (
    "company_id",
    "source",
    "source_variant",
    "metric",
    "source_metric_name",
    "fiscal_year",
    "fiscal_quarter",
    "canonical_period_end",
    "source_period_end",
    "unit",
    "source_scale_factor",
    "currency",
)
_VERSION_FIELDS = ("normalizer_version", "selection_policy_version")


def _present(item: Mapping, key: str, *, allow_none: bool = False) -> bool:
    if key not in item:
        return False
    return allow_none or item[key] not in (None, "")


def _parse_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=timezone.utc)
    else:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json_safe_dates(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _json_safe_dates(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe_dates(item) for item in value]
    if isinstance(value, list):
        return [_json_safe_dates(item) for item in value]
    return value


def _strong_identifier(evidence: Mapping):
    if evidence.get("source") == "SEC":
        identity_type = evidence.get("source_identity_type")
        if identity_type is not None and identity_type != "SEC_ACCESSION":
            return None
        return evidence.get("source_record_id")
    if evidence.get("source") == "YAHOO" and evidence.get("identity_is_immutable") is True:
        return (
            evidence.get("immutable_revision_id")
            or evidence.get("provider_revision_id")
            or evidence.get("source_record_id")
        )
    return None


def identity_strength(evidence: Mapping) -> str:
    """Return STRONG, LOGICAL, or AMBIGUOUS without inferring absent metadata."""

    strong_identifier = _strong_identifier(evidence)
    if strong_identifier and all(_present(evidence, key) for key in _STRONG_BASE_FIELDS):
        return "STRONG"
    currency_may_be_absent = evidence.get("unit") == "shares"
    if all(
        _present(
            evidence,
            key,
            allow_none=(key == "currency" and currency_may_be_absent),
        )
        for key in _LOGICAL_FIELDS
    ):
        return "LOGICAL"
    return "AMBIGUOUS"


def _strong_key(evidence: Mapping) -> tuple:
    return tuple(evidence.get(key) for key in _STRONG_BASE_FIELDS) + (
        _strong_identifier(evidence),
    )


def _logical_key(evidence: Mapping) -> tuple:
    return tuple(evidence.get(key) for key in _LOGICAL_FIELDS)


def _purpose_key(evidence: Mapping) -> tuple | None:
    fields = (
        "company_id",
        "metric",
        "fiscal_year",
        "fiscal_quarter",
        "canonical_period_end",
        "source_period_end",
    )
    if not all(_present(evidence, key) for key in fields):
        return None
    return tuple(evidence.get(key) for key in fields)


def _versions_compatible(first: Mapping, second: Mapping) -> bool:
    return all(
        _present(first, key)
        and _present(second, key)
        and first[key] == second[key]
        for key in _VERSION_FIELDS
    )


def _identity_token(evidence: Mapping) -> str:
    strength = identity_strength(evidence)
    key = _strong_key(evidence) if strength == "STRONG" else _logical_key(evidence)
    return json.dumps([strength, key], default=str, sort_keys=True, separators=(",", ":"))


def compare_evidence_lineage(
    legacy_lineage: Sequence[Mapping] | None,
    persisted_lineage: Sequence[Mapping] | None,
) -> dict:
    """Compare ordered evidence chains; order is contractual for derivatives."""

    legacy = list(legacy_lineage or [])
    persisted = list(persisted_lineage or [])
    if not legacy or not persisted:
        return {
            "relation": "AMBIGUOUS",
            "legacy_strengths": [identity_strength(item) for item in legacy],
            "persisted_strengths": [identity_strength(item) for item in persisted],
            "reason_codes": ["IDENTITY_AMBIGUOUS"],
            "order_changed": False,
        }

    legacy_strengths = [identity_strength(item) for item in legacy]
    persisted_strengths = [identity_strength(item) for item in persisted]
    legacy_tokens = [_identity_token(item) for item in legacy]
    persisted_tokens = [_identity_token(item) for item in persisted]
    order_changed = (
        legacy_tokens != persisted_tokens
        and sorted(legacy_tokens) == sorted(persisted_tokens)
    )
    base = {
        "legacy_strengths": legacy_strengths,
        "persisted_strengths": persisted_strengths,
        "order_changed": order_changed,
    }
    if len(legacy) != len(persisted) or "AMBIGUOUS" in legacy_strengths + persisted_strengths:
        return {
            "relation": "AMBIGUOUS",
            **base,
            "reason_codes": ["IDENTITY_AMBIGUOUS"],
        }

    all_strong = all(value == "STRONG" for value in legacy_strengths + persisted_strengths)
    strong_equal = all_strong and all(
        _strong_key(first) == _strong_key(second)
        for first, second in zip(legacy, persisted)
    )
    if strong_equal:
        if all(_versions_compatible(first, second) for first, second in zip(legacy, persisted)):
            return {
                "relation": "SAME_EVIDENCE",
                **base,
                "reason_codes": ["SAME_STRONG_IDENTITY"],
            }
        return {
            "relation": "AMBIGUOUS",
            **base,
            "reason_codes": ["INCOMPATIBLE_VERSIONS"],
        }

    if all(
        _logical_key(first) == _logical_key(second)
        for first, second in zip(legacy, persisted)
    ):
        return {
            "relation": "SAME_LOGICAL_SLOT",
            **base,
            "reason_codes": ["SAME_LOGICAL_SLOT"],
        }
    return {
        "relation": "DIFFERENT_EVIDENCE",
        **base,
        "reason_codes": [],
    }


def _unique_evidence(lineage_by_input: Mapping[str, Sequence[Mapping]] | None) -> list[dict]:
    result = []
    seen = set()
    for lineage in (lineage_by_input or {}).values():
        for evidence in lineage or []:
            marker = json.dumps(evidence, default=str, sort_keys=True, separators=(",", ":"))
            if marker not in seen:
                seen.add(marker)
                result.append(dict(evidence))
    return result


def calculate_freshness(
    persisted_lineage_by_input: Mapping[str, Sequence[Mapping]] | None,
    capture_metadata: Mapping,
) -> dict:
    """Calculate snapshot and evidence ages using supplied timestamps only."""

    evidence = _unique_evidence(persisted_lineage_by_input)
    persisted_read = _parse_datetime(capture_metadata.get("persisted_read_at"))
    live_completed = _parse_datetime(capture_metadata.get("legacy_capture_completed_at"))
    observed = [
        parsed
        for item in evidence
        if (parsed := _parse_datetime(item.get("observed_at"))) is not None
    ]
    snapshot_start = min(observed) if observed else None
    snapshot_end = max(observed) if observed else None
    age_reference = live_completed or persisted_read
    evidence_result = []
    for item in evidence:
        observed_at = _parse_datetime(item.get("observed_at"))
        source_available = _parse_datetime(item.get("source_available_at"))
        filed_date = _parse_date(item.get("filed_date"))
        if source_available is not None:
            available_value = _iso_datetime(source_available)
            available_for_age = source_available
            precision = "TIMESTAMP_PRECISION"
        elif item.get("source") == "SEC" and filed_date is not None:
            available_value = filed_date.isoformat()
            available_for_age = datetime.combine(filed_date, time.min, tzinfo=timezone.utc)
            precision = "DATE_PRECISION"
        else:
            available_value = None
            available_for_age = None
            precision = "UNKNOWN"
        evidence_result.append({
            "source": item.get("source"),
            "source_variant": item.get("source_variant"),
            "raw_id": item.get("raw_id"),
            "evidence_observed_at": _iso_datetime(observed_at),
            "evidence_available_at": available_value,
            "availability_precision": precision,
            "ingestion_age_seconds": (
                None
                if persisted_read is None or observed_at is None
                else (persisted_read - observed_at).total_seconds()
            ),
            "publication_age_seconds": (
                None
                if age_reference is None or available_for_age is None
                else (age_reference - available_for_age).total_seconds()
            ),
        })
    return {
        "persisted_read_at": _iso_datetime(persisted_read),
        "persisted_snapshot_time": _iso_datetime(snapshot_end),
        "persisted_snapshot_span": {
            "start": _iso_datetime(snapshot_start),
            "end": _iso_datetime(snapshot_end),
        },
        "capture_delta_seconds": (
            None
            if live_completed is None or persisted_read is None
            else (live_completed - persisted_read).total_seconds()
        ),
        "evidence": evidence_result,
        "sla_applied": False,
    }


def _newer_reason(legacy: Sequence[Mapping], persisted: Sequence[Mapping]) -> str | None:
    if len(legacy) != len(persisted) or not legacy:
        return None
    directions = set()
    for first, second in zip(legacy, persisted):
        pair_directions = set()
        for field in (
            "source_period_end",
            "canonical_period_end",
            "filed_date",
            "source_available_at",
        ):
            first_raw = first.get(field)
            second_raw = second.get(field)
            first_value = _parse_datetime(first_raw) if "at" in field else _parse_date(first_raw)
            second_value = _parse_datetime(second_raw) if "at" in field else _parse_date(second_raw)
            if first_value is None or second_value is None or first_value == second_value:
                continue
            pair_directions.add(1 if first_value > second_value else -1)
        if len(pair_directions) > 1:
            return None
        if pair_directions:
            directions.update(pair_directions)
            continue
        first_strength = identity_strength(first)
        second_strength = identity_strength(second)
        same_strong = (
            first_strength == second_strength == "STRONG"
            and _strong_key(first) == _strong_key(second)
            and _versions_compatible(first, second)
        )
        same_logical_capture = (
            first_strength == second_strength == "LOGICAL"
            and _logical_key(first) == _logical_key(second)
            and _parse_datetime(first.get("observed_at"))
            == _parse_datetime(second.get("observed_at"))
            and first.get("provider_id") == second.get("provider_id")
        )
        if not same_strong and not same_logical_capture:
            return None
    if directions == {1}:
        return "NEWER_LIVE_EVIDENCE"
    if directions == {-1}:
        return "NEWER_PERSISTED_EVIDENCE"
    return None


def _same_purpose(legacy: Sequence[Mapping], persisted: Sequence[Mapping]) -> bool:
    if len(legacy) != len(persisted) or not legacy:
        return False
    first = [_purpose_key(item) for item in legacy]
    second = [_purpose_key(item) for item in persisted]
    return None not in first + second and first == second


def _different_source_variant(legacy: Sequence[Mapping], persisted: Sequence[Mapping]) -> bool:
    return len(legacy) == len(persisted) and any(
        first.get("source") != second.get("source")
        or first.get("source_variant") != second.get("source_variant")
        for first, second in zip(legacy, persisted)
    )


def _different_capture(legacy: Sequence[Mapping], persisted: Sequence[Mapping]) -> bool:
    if len(legacy) != len(persisted):
        return False
    differs = False
    for first, second in zip(legacy, persisted):
        first_observed = _parse_datetime(first.get("observed_at"))
        second_observed = _parse_datetime(second.get("observed_at"))
        if first_observed is None or second_observed is None:
            return False
        if first_observed != second_observed:
            differs = True
    return differs


def _required_lineage_count(field: str, report: Mapping) -> int | None:
    fixed_counts = {
        "latest_eps_yoy_pct": 2,
        "previous_eps_yoy_pct": 2,
        "eps_acceleration_pp": 4,
        "latest_revenue_yoy_pct": 2,
        "previous_revenue_yoy_pct": 2,
        "revenue_acceleration_pp": 4,
        "latest_eps": 1,
        "eps_loss_to_profit": 2,
    }
    if field == "eps_yoy_pct":
        history = report.get(field)
        return len(history) * 2 if isinstance(history, list) else None
    return fixed_counts.get(field)


def _compare_input_lineage(
    field: str,
    legacy_report: Mapping,
    persisted_report: Mapping,
    legacy_lineage: Sequence[Mapping] | None,
    persisted_lineage: Sequence[Mapping] | None,
) -> dict:
    legacy = list(legacy_lineage or [])
    persisted = list(persisted_lineage or [])
    expected_legacy = _required_lineage_count(field, legacy_report)
    expected_persisted = _required_lineage_count(field, persisted_report)
    if (
        expected_legacy is None
        or expected_persisted is None
        or len(legacy) != expected_legacy
        or len(persisted) != expected_persisted
    ):
        return {
            "relation": "AMBIGUOUS",
            "legacy_strengths": [identity_strength(item) for item in legacy],
            "persisted_strengths": [identity_strength(item) for item in persisted],
            "reason_codes": ["INCOMPLETE_DERIVED_LINEAGE"],
            "order_changed": False,
        }
    return compare_evidence_lineage(legacy, persisted)


def _classify_input(
    field: str,
    legacy_report: Mapping,
    persisted_report: Mapping,
    legacy_lineage: Sequence[Mapping] | None,
    persisted_lineage: Sequence[Mapping] | None,
    field_comparison: Mapping,
    alignment: Mapping,
) -> dict:
    if field not in legacy_report:
        return {"category": "NOT_COMPARABLE", "reason_codes": ["MISSING_LEGACY"]}
    if field not in persisted_report:
        return {
            "category": "NOT_COMPARABLE",
            "reason_codes": [
                "MISSING_PERSISTED",
                "INGESTION_COMPLETENESS_NOT_ESTABLISHED",
            ],
        }
    reasons = list(alignment["reason_codes"])
    relation = alignment["relation"]
    if relation == "AMBIGUOUS":
        if "IDENTITY_AMBIGUOUS" not in reasons and "INCOMPATIBLE_VERSIONS" not in reasons:
            reasons.append("IDENTITY_AMBIGUOUS")
        return {"category": "NOT_COMPARABLE", "reason_codes": reasons}
    if relation == "SAME_EVIDENCE":
        if field_comparison.get("status") == "EXACT":
            return {"category": "MATCH", "reason_codes": reasons}
        if field_comparison.get("equivalent") is True:
            return {
                "category": "NUMERIC_EQUIVALENT",
                "reason_codes": reasons + ["VALUE_WITHIN_TOLERANCE"],
            }
        return {
            "category": "POSSIBLE_REGRESSION",
            "reason_codes": reasons + ["VALUE_OUTSIDE_TOLERANCE"],
        }

    legacy_items = list(legacy_lineage or [])
    persisted_items = list(persisted_lineage or [])
    newer = _newer_reason(legacy_items, persisted_items)
    if newer is not None:
        return {"category": "EXPECTED_DRIFT", "reason_codes": reasons + [newer]}
    immutable_identity_exists = "STRONG" in (
        alignment["legacy_strengths"] + alignment["persisted_strengths"]
    )
    if (
        relation == "SAME_LOGICAL_SLOT"
        and not immutable_identity_exists
        and _different_capture(legacy_items, persisted_items)
    ):
        return {
            "category": "CAPTURE_TIME_DIFFERENCE",
            "reason_codes": reasons + ["PROVIDER_REVISION_POSSIBLE"],
        }
    if _same_purpose(legacy_items, persisted_items) and _different_source_variant(
        legacy_items, persisted_items,
    ):
        return {
            "category": "SOURCE_DRIFT",
            "reason_codes": reasons + ["DIFFERENT_SOURCE_VARIANT"],
        }
    return {
        "category": "NOT_COMPARABLE",
        "reason_codes": reasons + ["DIFFERENT_EVIDENCE_UNEXPLAINED"],
    }


def _provider_failures(snapshot: Mapping | None) -> list[dict]:
    if not snapshot:
        return []
    return [copy.deepcopy(item) for item in snapshot.get("provider_failures", [])]


def _run_status(legacy_snapshot: Mapping | None, persisted_snapshot: Mapping | None) -> dict:
    legacy_status = None if legacy_snapshot is None else legacy_snapshot.get("acquisition_status", "UNKNOWN")
    persisted_status = None if persisted_snapshot is None else persisted_snapshot.get("acquisition_status", "UNKNOWN")
    failures = _provider_failures(legacy_snapshot) + _provider_failures(persisted_snapshot)
    if legacy_snapshot is None or persisted_snapshot is None:
        status = "FAILED"
    elif legacy_snapshot.get("fundamental_report") is None or persisted_snapshot.get("fundamental_report") is None:
        status = (
            "FAILED"
            if legacy_snapshot.get("fundamental_report") is None
            and persisted_snapshot.get("fundamental_report") is None
            else "PARTIAL"
        )
    elif failures or legacy_status != "COMPLETE" or persisted_status != "COMPLETE":
        status = "PARTIAL"
    else:
        status = "COMPLETE"
    return {
        "run_status": status,
        "legacy_status": legacy_status,
        "persisted_status": persisted_status,
        "provider_failures": failures,
    }


def _semantic_gaps(*collections: Iterable[Mapping] | None) -> list[dict]:
    result = []
    seen = set()
    for collection in collections:
        for gap in collection or []:
            item = copy.deepcopy(dict(gap))
            code = item.get("code")
            if code not in _SEMANTIC_GAP_CODES:
                raise ValueError(f"Unsupported semantic gap: {code}")
            relevance = item.get("score_relevant")
            item["score_relevant"] = relevance if isinstance(relevance, bool) else None
            marker = json.dumps(item, sort_keys=True, separators=(",", ":"))
            if marker not in seen:
                seen.add(marker)
                result.append(item)
    return result


def _deduplicated(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _global_classification(
    per_input: Mapping[str, Mapping],
    acquisition: Mapping,
    gaps: Sequence[Mapping],
) -> dict:
    reasons = []
    categories = []
    for field in FUNDAMENTAL_INPUT_KEYS:
        result = per_input.get(field)
        if result is not None:
            categories.append(result["category"])
            reasons.extend(result["reason_codes"])
    blocking_failures = [
        item for item in acquisition["provider_failures"]
        if item.get("affects_comparability") is not False
    ]
    if (
        acquisition.get("legacy_status") == "UNKNOWN"
        or acquisition.get("persisted_status") == "UNKNOWN"
    ):
        reasons.append("INGESTION_COMPLETENESS_NOT_ESTABLISHED")
    if gaps:
        primary = "SEMANTIC_GAP"
        reasons = [gap["code"] for gap in gaps] + reasons
    elif blocking_failures:
        primary = "NOT_COMPARABLE"
        reasons = ["PROVIDER_FAILURE"] + reasons
    else:
        precedence = (
            "NOT_COMPARABLE",
            "POSSIBLE_REGRESSION",
            "EXPECTED_DRIFT",
            "CAPTURE_TIME_DIFFERENCE",
            "SOURCE_DRIFT",
            "NUMERIC_EQUIVALENT",
            "MATCH",
        )
        primary = next((item for item in precedence if item in categories), "NOT_COMPARABLE")
    return {
        "primary": primary,
        "categories_present": _deduplicated(categories),
        "reason_codes": _deduplicated(reasons),
        "per_input": copy.deepcopy(dict(per_input)),
    }


def _severity(classification: Mapping, gaps: Sequence[Mapping]) -> str:
    primary = classification["primary"]
    if primary == "SEMANTIC_GAP":
        if any(gap.get("score_relevant") is True for gap in gaps):
            return "REVIEW_REQUIRED"
        if any(gap.get("score_relevant") is None for gap in gaps):
            return "WARNING"
        explicit = [gap.get("severity") for gap in gaps if gap.get("severity") in {"INFO", "WARNING"}]
        return "WARNING" if "WARNING" in explicit else "INFO"
    if primary == "POSSIBLE_REGRESSION":
        return "REVIEW_REQUIRED"
    if primary in {"SOURCE_DRIFT", "CAPTURE_TIME_DIFFERENCE", "NOT_COMPARABLE"}:
        return "WARNING"
    return "INFO"


def _score_isolation(
    legacy_report: Mapping | None,
    persisted_report: Mapping | None,
    shared_live_complements: Mapping | None,
) -> dict:
    if shared_live_complements is None:
        return {"status": "NOT_RUN", "reason_codes": ["SHARED_LIVE_COMPLEMENTS_NOT_PROVIDED"]}
    if legacy_report is None or persisted_report is None:
        return {"status": "NOT_RUN", "reason_codes": ["MISSING_FUNDAMENTAL_REPORT"]}
    fixture = {
        "shared_complements": {
            "shared_for_score_isolation": True,
            "independently_reconstructed_by_new_architecture": False,
            "source": "legacy_live_single_capture",
            "values": copy.deepcopy(dict(shared_live_complements)),
        }
    }
    return compare_score_equivalence(
        copy.deepcopy(dict(legacy_report)),
        copy.deepcopy(dict(persisted_report)),
        fixture,
    )


def diagnose_c_snapshots(
    legacy_snapshot: Mapping | None,
    persisted_snapshot: Mapping | None,
    *,
    capture_metadata: Mapping,
    shared_live_complements: Mapping | None = None,
    semantic_gaps: Sequence[Mapping] | None = None,
) -> dict:
    """Diagnose two supplied snapshots without acquiring or persisting data."""

    legacy_report = None if legacy_snapshot is None else legacy_snapshot.get("fundamental_report")
    persisted_report = None if persisted_snapshot is None else persisted_snapshot.get("fundamental_report")
    legacy_lineage = {} if legacy_snapshot is None else legacy_snapshot.get("lineage_by_input", {})
    persisted_lineage = {} if persisted_snapshot is None else persisted_snapshot.get("lineage_by_input", {})
    acquisition = _run_status(legacy_snapshot, persisted_snapshot)
    gaps = _semantic_gaps(
        None if legacy_snapshot is None else legacy_snapshot.get("semantic_gaps"),
        None if persisted_snapshot is None else persisted_snapshot.get("semantic_gaps"),
        semantic_gaps,
    )

    if legacy_report is not None and persisted_report is not None:
        fundamental = compare_fundamental_contract(
            copy.deepcopy(dict(legacy_report)),
            copy.deepcopy(dict(persisted_report)),
            legacy_provenance=copy.deepcopy(dict(legacy_lineage)),
            new_provenance=copy.deepcopy(dict(persisted_lineage)),
        )
        alignments = {
            field: _compare_input_lineage(
                field,
                legacy_report,
                persisted_report,
                legacy_lineage.get(field),
                persisted_lineage.get(field),
            )
            for field in FUNDAMENTAL_INPUT_KEYS
        }
        per_input = {
            field: _classify_input(
                field,
                legacy_report,
                persisted_report,
                legacy_lineage.get(field),
                persisted_lineage.get(field),
                fundamental["fields"][field],
                alignments[field],
            )
            for field in FUNDAMENTAL_INPUT_KEYS
        }
        differences = copy.deepcopy(fundamental["differences"])
    else:
        fundamental = None
        alignments = {}
        reason = "MISSING_LEGACY" if legacy_report is None else "MISSING_PERSISTED"
        per_input = {
            field: {"category": "NOT_COMPARABLE", "reason_codes": [reason]}
            for field in FUNDAMENTAL_INPUT_KEYS
        }
        differences = []

    classification = _global_classification(per_input, acquisition, gaps)
    result = {
        "metadata": _json_safe_dates(copy.deepcopy(dict(capture_metadata))),
        "acquisition_status": acquisition,
        "legacy_live": copy.deepcopy(legacy_report),
        "new_persisted": copy.deepcopy(persisted_report),
        "evidence_alignment": alignments,
        "fundamental_comparison": fundamental,
        "fundamental_differences": differences,
        "freshness": calculate_freshness(persisted_lineage, capture_metadata),
        "score_isolation": _score_isolation(
            legacy_report, persisted_report, shared_live_complements,
        ),
        "legacy_end_to_end_score": (
            None
            if legacy_snapshot is None
            else copy.deepcopy(legacy_snapshot.get("legacy_end_to_end_score"))
        ),
        "semantic_gaps": gaps,
        "classification": classification,
        "severity": _severity(classification, gaps),
        "end_to_end_equivalent": False,
        "end_to_end_status": "NOT_ESTABLISHED",
    }
    return _json_safe_dates(result)
