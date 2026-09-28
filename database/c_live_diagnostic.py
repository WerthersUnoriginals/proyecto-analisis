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
from database.c_dual_run_contract import (
    FUNDAMENTAL_INPUT_KEYS,
    INDEPENDENT_C_INPUT_KEYS,
)
from database.c_data_integrity import VALID_DATA_INTEGRITY_STATUSES
from database.split_integrity import VALID_SPLIT_INTEGRITY_STATUSES


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

    if evidence.get("source") == "SEC":
        identity_type = evidence.get("source_identity_type")
        if identity_type not in (None, "SEC_ACCESSION"):
            return "AMBIGUOUS"
        if evidence.get("source_identity_error"):
            return "AMBIGUOUS"
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
    values = []
    for key in _STRONG_BASE_FIELDS:
        value = evidence.get(key)
        if key in {"source_period_start", "source_period_end"}:
            parsed = _parse_date(value)
            value = parsed.isoformat() if parsed is not None else value
        values.append(value)
    return tuple(values) + (
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


def _processing_version_status(first: Mapping, second: Mapping) -> str:
    first_version = first.get("normalizer_version")
    second_version = second.get("normalizer_version")
    if first_version and second_version:
        return "SAME_VERSION" if first_version == second_version else "DIFFERENT_VERSION"
    if first_version is None and second_version:
        return "LEGACY_VERSION_UNKNOWN"
    if first_version and second_version is None:
        return "PROCESSING_VERSION_UNKNOWN"
    return "NOT_APPLICABLE"


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
        "processing_version_status": (
            "SAME_VERSION"
            if all(
                _processing_version_status(first, second) == "SAME_VERSION"
                for first, second in zip(legacy, persisted)
            )
            else (
                _processing_version_status(legacy[0], persisted[0])
                if len(legacy) == len(persisted) == 1
                else "MIXED"
            )
        ),
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
        return {
            "relation": "SAME_EVIDENCE",
            **base,
            "reason_codes": ["SAME_STRONG_IDENTITY"],
        }

    if all_strong:
        return {
            "relation": "DIFFERENT_EVIDENCE",
            **base,
            "reason_codes": ["DIFFERENT_STRONG_IDENTITY"],
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


def _score_relevant_lineage(
    field: str,
    report: Mapping,
    lineage: Sequence[Mapping],
) -> tuple[list[Mapping], int] | None:
    """Select the exact C v1.2-exp EPS history window from flattened lineage."""
    if field != "eps_yoy_pct":
        return list(lineage), len(lineage)
    history = report.get(field)
    if not isinstance(history, list):
        return None
    expected = len(history) * 2
    if len(lineage) != expected:
        return None
    records = min(4, len(history))
    width = records * 2
    return list(lineage[-width:]), records


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
    if field == "eps_yoy_pct":
        legacy_selected = _score_relevant_lineage(field, legacy_report, legacy)
        persisted_selected = _score_relevant_lineage(field, persisted_report, persisted)
        if legacy_selected is None or persisted_selected is None:
            return {
                "relation": "AMBIGUOUS",
                "legacy_strengths": [identity_strength(item) for item in legacy],
                "persisted_strengths": [identity_strength(item) for item in persisted],
                "reason_codes": ["INCOMPLETE_DERIVED_LINEAGE"],
                "order_changed": False,
            }
        full_relation = compare_evidence_lineage(legacy, persisted)
        score_relation = compare_evidence_lineage(
            legacy_selected[0], persisted_selected[0],
        )
        score_relation["full_history_relation"] = (
            "EQUIVALENT" if full_relation["relation"] == "SAME_EVIDENCE" else "DIFFERENT"
        )
        score_relation["score_relevant_relation"] = score_relation["relation"]
        return score_relation
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
        score_relevant = field_comparison.get("score_relevant_history")
        comparison_equivalent = field_comparison.get("equivalent")
        if field == "eps_yoy_pct" and isinstance(score_relevant, Mapping):
            comparison_equivalent = score_relevant.get("equivalent")
        if field_comparison.get("status") == "EXACT":
            return {"category": "MATCH", "reason_codes": reasons}
        if comparison_equivalent is True:
            if field == "eps_yoy_pct" and field_comparison.get("equivalent") is not True:
                reasons = reasons + ["FULL_HISTORY_DIFFERENCE_NON_SCORE_RELEVANT"]
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


def _classify_integrity_input(
    field: str,
    legacy_report: Mapping | None,
    persisted_report: Mapping | None,
) -> dict:
    """Compare one independently reconstructed categorical C input."""
    if not isinstance(legacy_report, Mapping) or field not in legacy_report:
        return {"category": "NOT_COMPARABLE", "reason_codes": ["MISSING_LEGACY"]}
    if not isinstance(persisted_report, Mapping) or field not in persisted_report:
        return {"category": "NOT_COMPARABLE", "reason_codes": ["MISSING_PERSISTED"]}
    valid_statuses = (
        VALID_DATA_INTEGRITY_STATUSES
        if field == "data_integrity"
        else VALID_SPLIT_INTEGRITY_STATUSES
    )
    legacy_valid = isinstance(legacy_report[field], str) and legacy_report[field] in valid_statuses
    persisted_valid = isinstance(persisted_report[field], str) and persisted_report[field] in valid_statuses
    if not legacy_valid or not persisted_valid:
        return {
            "category": "NOT_COMPARABLE",
            "reason_codes": ["INTEGRITY_STATUS_INVALID"],
            "legacy_value_valid": legacy_valid,
            "persisted_value_valid": persisted_valid,
        }
    if legacy_report[field] == persisted_report[field]:
        return {
            "category": "MATCH",
            "reason_codes": [],
            "legacy_value_valid": True,
            "persisted_value_valid": True,
        }
    return {
        "category": "POSSIBLE_REGRESSION",
        "reason_codes": ["INTEGRITY_SEMANTIC_VALUE_DIFFERENCE"],
        "legacy_value_valid": True,
        "persisted_value_valid": True,
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
    if (
        persisted_report is not None
        and persisted_report.get("independently_reconstructed_by_new_architecture") is True
    ):
        from database.c_dual_run import compare_score_equivalence_independent

        return compare_score_equivalence_independent(
            copy.deepcopy(dict(legacy_report or {})),
            copy.deepcopy(dict(persisted_report)),
        )
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


def _final_end_to_end_gate(
    persisted_report: Mapping | None,
    classification: Mapping,
    alignments: Mapping[str, Mapping],
    score_isolation: Mapping,
    semantic_gaps: Sequence[Mapping],
    acquisition: Mapping,
) -> dict:
    """Aggregate the score-relevant C v1.2-exp equivalence contract.

    Full payload/history comparison remains diagnostic only; this gate uses
    the exact inputs and evidence subset consumed by the scorer.
    """
    report = persisted_report if isinstance(persisted_report, Mapping) else {}
    contract = report.get("c_input_contract", {})
    contract_inputs = contract.get("inputs", {}) if isinstance(contract, Mapping) else {}
    contract_inputs = contract_inputs if isinstance(contract_inputs, Mapping) else {}
    required_inputs = tuple(INDEPENDENT_C_INPUT_KEYS)
    required_input_set = set(required_inputs)
    per_input = classification.get("per_input", {})
    score_categories = {
        "MATCH", "NUMERIC_EQUIVALENT",
    }
    provider_failures = acquisition.get("provider_failures", [])
    input_validation = {}
    reason_codes = []
    for field in required_inputs:
        entry = contract_inputs.get(field)
        entry_is_mapping = isinstance(entry, Mapping)
        required_fields_present = (
            entry_is_mapping
            and "value" in entry
            and "independently_reconstructed" in entry
        )
        structurally_valid = bool(required_fields_present)
        independent = bool(
            structurally_valid
            and entry.get("independently_reconstructed") is True
        )
        value_matches_report = bool(
            structurally_valid
            and field in report
            and entry.get("value") == report.get(field)
        )
        comparison = per_input.get(field)
        semantically_equivalent = bool(
            isinstance(comparison, Mapping)
            and comparison.get("category") in score_categories
        )
        item_reasons = []
        if field in {"data_integrity", "split_integrity_status"}:
            valid_statuses = (
                VALID_DATA_INTEGRITY_STATUSES
                if field == "data_integrity"
                else VALID_SPLIT_INTEGRITY_STATUSES
            )
            contract_value_valid = (
                entry_is_mapping
                and isinstance(entry.get("value"), str)
                and entry.get("value") in valid_statuses
            )
            persisted_value_valid = (
                isinstance(report.get(field), str)
                and report.get(field) in valid_statuses
            )
            comparison_values_valid = (
                isinstance(comparison, Mapping)
                and comparison.get("legacy_value_valid") is True
                and comparison.get("persisted_value_valid") is True
            )
            if not (contract_value_valid and persisted_value_valid and comparison_values_valid):
                semantically_equivalent = False
                item_reasons.append("INTEGRITY_STATUS_INVALID")
        if not entry_is_mapping or not required_fields_present:
            item_reasons.append("C_INPUT_CONTRACT_ENTRY_INVALID")
        if not independent:
            item_reasons.append("C_INPUT_NOT_INDEPENDENT")
        if not value_matches_report:
            item_reasons.append("C_INPUT_CONTRACT_VALUE_MISMATCH")
        if not semantically_equivalent:
            item_reasons.append("C_INPUT_SEMANTIC_MISMATCH")
        if isinstance(comparison, Mapping):
            item_reasons.extend(comparison.get("reason_codes", ()))
        reason_codes.extend(item_reasons)
        input_validation[field] = {
            "structurally_valid": structurally_valid,
            "independently_reconstructed": independent,
            "value_matches_report": value_matches_report,
            "semantically_equivalent": semantically_equivalent,
            "valid": (
                structurally_valid
                and independent
                and value_matches_report
                and semantically_equivalent
            ),
            "reason_codes": item_reasons,
        }

    exact_required_input_count = (
        len(contract_inputs) == len(required_inputs)
        and set(contract_inputs) == required_input_set
    )
    if not exact_required_input_count:
        reason_codes.append("C_INPUT_CONTRACT_COUNT_INVALID")
    valid_count = sum(
        item["structurally_valid"] for item in input_validation.values()
    )
    independent_count = sum(
        item["independently_reconstructed"] for item in input_validation.values()
    )
    equivalent_count = sum(
        item["valid"] for item in input_validation.values()
    )
    contract_validation = {
        "required_count": len(required_inputs),
        "present_count": len(contract_inputs),
        "valid_count": valid_count,
        "independent_count": independent_count,
        "equivalent_count": equivalent_count,
        "per_input": input_validation,
    }
    all_inputs_valid_and_independent = (
        exact_required_input_count
        and valid_count == len(required_inputs)
        and independent_count == len(required_inputs)
        and report.get("independently_reconstructed_by_new_architecture") is True
    )
    all_input_values_equivalent = equivalent_count == len(required_inputs)
    integrity_semantics_equivalent = all(
        input_validation[field]["semantically_equivalent"]
        for field in ("data_integrity", "split_integrity_status")
    )
    ingestion_completeness_established = (
        "INGESTION_COMPLETENESS_NOT_ESTABLISHED"
        not in classification.get("reason_codes", ())
    )
    conditions = {
        "no_score_relevant_semantic_gaps": not any(
            gap.get("score_relevant") is not False for gap in semantic_gaps
        ),
        "exact_required_input_count": exact_required_input_count,
        "all_eleven_inputs_independent": all_inputs_valid_and_independent,
        "no_shared_complements": (
            score_isolation.get("independent_inputs") is True
            and score_isolation.get("shared_complements") is None
        ),
        "score_relevant_values_equivalent": all_input_values_equivalent,
        "score_relevant_evidence_equivalent": all(
            alignments.get(field, {}).get("relation") == "SAME_EVIDENCE"
            for field in FUNDAMENTAL_INPUT_KEYS
        ),
        "score_equivalent": score_isolation.get("equivalent") is True,
        "integrity_semantics_equivalent": integrity_semantics_equivalent,
        "ingestion_completeness_established": ingestion_completeness_established,
        "integrity_inputs_independent": (
            all(
                input_validation[field]["valid"]
                for field in ("data_integrity", "split_integrity_status")
            )
        ),
        "provider_failures_non_blocking": all(
            item.get("affects_comparability") is False
            for item in provider_failures
        ),
    }
    equivalent = all(conditions.values())
    return {
        "equivalent": equivalent,
        "status": "ESTABLISHED" if equivalent else "NOT_ESTABLISHED",
        "conditions": conditions,
        "input_contract_validation": contract_validation,
        "reason_codes": _deduplicated(reason_codes),
        "full_history_differences_allowed": True,
    }


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
    independent_persisted = bool(
        isinstance(persisted_snapshot, Mapping)
        and isinstance(persisted_snapshot.get("fundamental_report"), Mapping)
        and persisted_snapshot["fundamental_report"].get(
            "independently_reconstructed_by_new_architecture"
        ) is True
    )
    legacy_gaps = (
        None if legacy_snapshot is None else legacy_snapshot.get("semantic_gaps")
    )
    if independent_persisted:
        legacy_gaps = [
            gap for gap in (legacy_gaps or [])
            if gap.get("code") not in {
                "CORPORATE_ACTIONS_NOT_RECONSTRUCTED",
                "DATA_INTEGRITY_NOT_RECONSTRUCTED",
            }
        ]
    gaps = _semantic_gaps(
        legacy_gaps,
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

    per_input.update({
        field: _classify_integrity_input(field, legacy_report, persisted_report)
        for field in ("data_integrity", "split_integrity_status")
    })

    classification = _global_classification(per_input, acquisition, gaps)
    score_isolation = _score_isolation(
        legacy_report, persisted_report, shared_live_complements,
    )
    end_to_end_gate = _final_end_to_end_gate(
        persisted_report,
        classification,
        alignments,
        score_isolation,
        gaps,
        acquisition,
    )
    result = {
        "metadata": _json_safe_dates(copy.deepcopy(dict(capture_metadata))),
        "acquisition_status": acquisition,
        "legacy_live": copy.deepcopy(legacy_report),
        "new_persisted": copy.deepcopy(persisted_report),
        "evidence_alignment": alignments,
        "fundamental_comparison": fundamental,
        "fundamental_differences": differences,
        "freshness": calculate_freshness(persisted_lineage, capture_metadata),
        "score_isolation": score_isolation,
        "legacy_end_to_end_score": (
            None
            if legacy_snapshot is None
            else copy.deepcopy(legacy_snapshot.get("legacy_end_to_end_score"))
        ),
        "semantic_gaps": gaps,
        "classification": classification,
        "severity": _severity(classification, gaps),
        "end_to_end_equivalent": end_to_end_gate["equivalent"],
        "end_to_end_status": end_to_end_gate["status"],
        "end_to_end_gate": end_to_end_gate,
    }
    return _json_safe_dates(result)
