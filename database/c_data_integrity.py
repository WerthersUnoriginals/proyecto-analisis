"""Legacy-compatible source consistency and point-in-time evidence loading.

This module deliberately does not call the legacy provider implementation.  It
operates on normalized observations already persisted by the v2 normalizers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Mapping, Sequence


LEGACY_CONSISTENCY_VERSION = "legacy-source-consistency-v1"
MAX_ALIGNMENT_DAYS = 35
OK_TOLERANCE_PCT = Decimal("1")
ACCOUNTING_TOLERANCE_PCT = Decimal("5")
VALID_DATA_INTEGRITY_STATUSES = frozenset({
    "VERIFIED",
    "REVIEW_REQUIRED",
    "VERIFIED_WITH_ACCOUNTING_DIFFERENCE",
    "VERIFIED_WITH_BASIC_SHARES_FALLBACK",
    "VERIFIED_WITH_PARTIAL_CORE_DATA",
    "VERIFIED_WITH_ACCOUNTING_DIFFERENCE_AND_BASIC_SHARES_FALLBACK",
    "VERIFIED_WITH_ACCOUNTING_DIFFERENCE_AND_PARTIAL_CORE_DATA",
    "VERIFIED_WITH_BASIC_SHARES_FALLBACK_AND_PARTIAL_CORE_DATA",
    "VERIFIED_WITH_ACCOUNTING_DIFFERENCE_AND_BASIC_SHARES_FALLBACK_AND_PARTIAL_CORE_DATA",
})
EPS_METRICS = {"EPS", "EPS_DILUTED", "EPS_BASIC", "EPS_UNSPECIFIED"}

_YAHOO_VARIANT_PRIORITY = (
    "yahoo.fundamentals_timeseries",
    "yfinance.quarterly_income_stmt",
)
_YFINANCE_EPS_PRIORITY = (
    "Diluted EPS",
    "DilutedEPS",
    "Basic EPS",
    "BasicEPS",
)
_DILUTED_EPS_NAMES = {"Diluted EPS", "DilutedEPS", "quarterlyDilutedEPS"}
_BASIC_EPS_NAMES = {"Basic EPS", "BasicEPS"}

_NORMALIZED_COLUMNS = (
    "id", "company_id", "metric", "source", "source_variant", "observation_kind",
    "value", "unit", "currency", "source_period_start", "source_period_end",
    "canonical_period_end", "series_date", "fiscal_year", "fiscal_quarter",
    "filed_date", "source_available_at", "observed_at", "raw_id",
    "normalizer_version", "intrinsic_quality_status", "intrinsic_quality_reasons",
    "selection_eligibility", "alignment_method", "alignment_days",
    "alignment_reference_id", "source_metric_name", "source_unit",
    "source_scale_factor", "created_at",
)


@dataclass(frozen=True)
class ConsistencyMatch:
    sec_date: date
    yahoo_date: date
    distance_days: int
    sec_value: Decimal
    yahoo_value: Decimal
    difference_pct: Decimal


@dataclass(frozen=True)
class LegacyConsistencyResult:
    status: str
    max_diff_pct: float | None
    avg_diff_pct: float | None
    matched_count: int
    matches: tuple[ConsistencyMatch, ...] = ()
    reasons: tuple[str, ...] = ()
    version: str = LEGACY_CONSISTENCY_VERSION


@dataclass(frozen=True)
class DataIntegrityAggregation:
    data_integrity: str
    warnings: tuple[str, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class IntegrityReconstruction:
    data_integrity: str
    data_quality: str
    split_integrity_status: str
    current_yoy_crosses_split: bool | None
    consistency: Mapping[str, LegacyConsistencyResult]
    shares_quality: str
    warnings: tuple[str, ...]
    diagnostics: tuple[str, ...]


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _as_decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _observation_date(row: Mapping[str, object]) -> date | None:
    return _as_date(row.get("source_period_end") or row.get("series_date"))


def _observation_value(row: Mapping[str, object]) -> Decimal | None:
    return _as_decimal(row.get("value"))


def _is_eps(row: Mapping[str, object], metric: str | None) -> bool:
    return (metric in EPS_METRICS) or row.get("metric") in EPS_METRICS


def _legacy_alias_rank(row: Mapping[str, object]) -> int:
    name = str(row.get("source_metric_name") or "")
    try:
        return _YFINANCE_EPS_PRIORITY.index(name)
    except ValueError:
        return len(_YFINANCE_EPS_PRIORITY)


def _deduplicate_source_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], list[str]]:
    """Keep the latest visible row per semantic date; reject same-time conflicts."""
    grouped: dict[tuple[date, str], list[Mapping[str, object]]] = {}
    reasons: list[str] = []
    for row in rows:
        day = _observation_date(row)
        if day is None or _observation_value(row) is None:
            continue
        grouped.setdefault((day, str(row.get("source_variant") or "")), []).append(row)
    chosen: list[Mapping[str, object]] = []
    for (day, _variant), candidates in grouped.items():
        candidates = sorted(
            candidates,
            key=lambda row: (
                row.get("observed_at") or datetime.min.replace(tzinfo=timezone.utc),
                row.get("raw_id") or 0,
            ),
        )
        latest_time = candidates[-1].get("observed_at")
        latest = [row for row in candidates if row.get("observed_at") == latest_time]
        values = {_observation_value(row) for row in latest}
        if len(values) > 1:
            reasons.append(f"CONFLICTING_VISIBLE_ROWS:{day.isoformat()}")
            continue
        chosen.append(latest[-1])
    return chosen, reasons


def _resolve_yahoo_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    metric: str | None,
) -> tuple[list[Mapping[str, object]], list[str]]:
    """Apply legacy Yahoo variant priority without erasing EPS semantics."""
    normalized, reasons = _deduplicate_source_rows(rows)
    ts = [row for row in normalized if row.get("source_variant") == _YAHOO_VARIANT_PRIORITY[0]]
    yf = [row for row in normalized if row.get("source_variant") == _YAHOO_VARIANT_PRIORITY[1]]
    if _is_eps({}, metric) or any(_is_eps(row, metric) for row in normalized):
        for row in normalized:
            name = str(row.get("source_metric_name") or "")
            row_metric = row.get("metric")
            if row_metric in {"EPS_BASIC", "EPS_UNSPECIFIED"} or name in _BASIC_EPS_NAMES or name == "legacy.unknown":
                row_date = _observation_date(row)
                has_ts = any(
                    _observation_date(other) is not None
                    and row_date is not None
                    and abs((_observation_date(other) - row_date).days) <= MAX_ALIGNMENT_DAYS
                    for other in ts
                )
                if not has_ts:
                    reasons.append("EPS_SEMANTICS_UNPROVEN")
        ts = [
            row for row in ts
            if str(row.get("source_metric_name") or "") in {"quarterlyDilutedEPS", "Diluted EPS", "DilutedEPS"}
            or row.get("metric") == "EPS_DILUTED"
        ]
        yf = [
            row for row in yf
            if str(row.get("source_metric_name") or "") in _DILUTED_EPS_NAMES
            or row.get("metric") == "EPS_DILUTED"
            or str(row.get("source_metric_name") or "") in _BASIC_EPS_NAMES
            or row.get("metric") == "EPS_BASIC"
            or row.get("metric") == "EPS_UNSPECIFIED"
        ]
    selected = list(ts)
    for row in sorted(yf, key=lambda item: (_observation_date(item) or date.min, _legacy_alias_rank(item))):
        row_date = _observation_date(row)
        if row_date is None:
            continue
        if any(
            _observation_date(other) is not None
            and abs((_observation_date(other) - row_date).days) <= MAX_ALIGNMENT_DAYS
            for other in ts
        ):
            continue
        same_day = [other for other in selected if _observation_date(other) == row_date]
        if same_day:
            best = min([*same_day, row], key=_legacy_alias_rank)
            selected = [other for other in selected if _observation_date(other) != row_date]
            selected.append(best)
        else:
            selected.append(row)
    return selected, reasons


def _nearest_sec(
    sec_rows: Sequence[Mapping[str, object]],
    yahoo_date: date,
    *,
    candidate_order_demonstrated: bool,
) -> tuple[Mapping[str, object] | None, str | None]:
    candidates = []
    for row in sec_rows:
        sec_date = _observation_date(row)
        if sec_date is None:
            continue
        distance = abs((sec_date - yahoo_date).days)
        if distance <= MAX_ALIGNMENT_DAYS:
            candidates.append((distance, row))
    if not candidates:
        return None, None
    distance = min(item[0] for item in candidates)
    nearest = [row for item_distance, row in candidates if item_distance == distance]
    values = {_observation_value(row) for row in nearest}
    if len(nearest) > 1 and len(values) > 1 and not candidate_order_demonstrated:
        return None, "SEC_NEAREST_TIE"
    if len(nearest) > 1 and candidate_order_demonstrated:
        nearest = sorted(nearest, key=lambda row: row.get("legacy_order", row.get("raw_id", 0)))
    return nearest[0], None


def compare_legacy_source_consistency(
    sec_observations: Iterable[Mapping[str, object]],
    yahoo_observations: Iterable[Mapping[str, object]],
    *,
    metric: str | None = None,
    candidate_order_demonstrated: bool = False,
) -> LegacyConsistencyResult:
    """Reproduce the numeric legacy SEC/Yahoo consistency rule."""
    sec_rows = [row for row in sec_observations if row.get("source", "SEC") == "SEC"]
    yahoo_rows = [row for row in yahoo_observations if row.get("source", "YAHOO") == "YAHOO"]
    yahoo_rows, semantic_reasons = _resolve_yahoo_rows(yahoo_rows, metric=metric)
    reasons = list(semantic_reasons)
    matches: list[ConsistencyMatch] = []
    for yahoo in yahoo_rows:
        yahoo_date = _observation_date(yahoo)
        yahoo_value = _observation_value(yahoo)
        if yahoo_date is None or yahoo_value is None:
            continue
        sec, tie_reason = _nearest_sec(
            sec_rows, yahoo_date,
            candidate_order_demonstrated=candidate_order_demonstrated,
        )
        if tie_reason:
            reasons.append(tie_reason)
            continue
        if sec is None:
            continue
        sec_date = _observation_date(sec)
        sec_value = _observation_value(sec)
        if sec_date is None or sec_value is None:
            continue
        denominator = max(abs(sec_value), abs(yahoo_value), Decimal("1e-9"))
        diff = abs(sec_value - yahoo_value) / denominator * Decimal("100")
        matches.append(ConsistencyMatch(
            sec_date=sec_date,
            yahoo_date=yahoo_date,
            distance_days=abs((sec_date - yahoo_date).days),
            sec_value=sec_value,
            yahoo_value=yahoo_value,
            difference_pct=diff,
        ))
    if reasons:
        return LegacyConsistencyResult(
            "AMBIGUOUS", _max(matches), _average(matches), len(matches),
            tuple(matches), tuple(dict.fromkeys(reasons)),
        )
    if not matches:
        return LegacyConsistencyResult("N/D", None, None, 0)
    maximum = _max(matches)
    if maximum is not None and maximum <= OK_TOLERANCE_PCT:
        status = "OK"
    elif maximum is not None and maximum <= ACCOUNTING_TOLERANCE_PCT:
        status = "DISCREPANCIA_CONTABLE"
    else:
        status = "DISCREPANCIA_ALTA"
    return LegacyConsistencyResult(
        status, maximum, _average(matches), len(matches), tuple(matches), (),
    )


def _max(matches: Sequence[ConsistencyMatch]) -> float | None:
    return None if not matches else float(max(item.difference_pct for item in matches))


def _average(matches: Sequence[ConsistencyMatch]) -> float | None:
    return None if not matches else float(sum(item.difference_pct for item in matches) / len(matches))


def classify_data_quality(
    *,
    latest_eps_yoy_pct=None,
    previous_eps_yoy_pct=None,
    eps_acceleration_pp=None,
    latest_revenue_yoy_pct=None,
    previous_revenue_yoy_pct=None,
    revenue_acceleration_pp=None,
    current_yoy_crosses_split: bool | None = False,
    split_integrity_status: str = "NO_RECENT_SPLITS",
) -> str:
    """Classify the six central scalar inputs using the legacy contract."""
    core = (
        latest_eps_yoy_pct, previous_eps_yoy_pct, eps_acceleration_pp,
        latest_revenue_yoy_pct, previous_revenue_yoy_pct, revenue_acceleration_pp,
    )
    if all(value is not None for value in core):
        quality = "completa"
    elif latest_eps_yoy_pct is not None and latest_revenue_yoy_pct is not None:
        quality = "suficiente"
    elif latest_eps_yoy_pct is not None or latest_revenue_yoy_pct is not None:
        quality = "parcial"
    else:
        quality = "insuficiente"
    if current_yoy_crosses_split and split_integrity_status in {
        "UNADJUSTED_DETECTED", "REVIEW_REQUIRED", "UNKNOWN",
    }:
        return "revision_split"
    return quality


def _consistency_status(value: object) -> str:
    if isinstance(value, LegacyConsistencyResult):
        return value.status
    if isinstance(value, Mapping):
        return str(value.get("status", "AMBIGUOUS"))
    return str(value)


def aggregate_data_integrity(
    data_quality: str,
    split_integrity_status: str,
    consistency: Mapping[str, object],
    shares_quality: str,
) -> DataIntegrityAggregation:
    """Aggregate independent evidence without consulting the legacy path."""
    statuses = {_consistency_status(value) for value in consistency.values()}
    diagnostics: list[str] = []
    if data_quality in {"insuficiente", "revision_split"}:
        diagnostics.append(f"DATA_QUALITY:{data_quality}")
        return DataIntegrityAggregation("REVIEW_REQUIRED", (), tuple(diagnostics))
    if split_integrity_status in {"UNADJUSTED_DETECTED", "REVIEW_REQUIRED", "UNKNOWN"}:
        diagnostics.append(f"SPLIT_STATUS:{split_integrity_status}")
        return DataIntegrityAggregation("REVIEW_REQUIRED", (), tuple(diagnostics))
    if "DISCREPANCIA_ALTA" in statuses or "AMBIGUOUS" in statuses or "REVIEW_REQUIRED" in statuses:
        if "DISCREPANCIA_ALTA" in statuses:
            diagnostics.append("HIGH_SOURCE_DISCREPANCY")
        if "AMBIGUOUS" in statuses or "REVIEW_REQUIRED" in statuses:
            diagnostics.append("AMBIGUOUS_SOURCE_EVIDENCE")
        return DataIntegrityAggregation("REVIEW_REQUIRED", (), tuple(diagnostics))
    warnings: list[str] = []
    if "DISCREPANCIA_CONTABLE" in statuses:
        warnings.append("ACCOUNTING_DIFFERENCE")
    if shares_quality == "BASIC_FALLBACK":
        warnings.append("BASIC_SHARES_FALLBACK")
    if data_quality != "completa":
        warnings.append("PARTIAL_CORE_DATA")
    if not warnings:
        return DataIntegrityAggregation("VERIFIED", (), ())
    return DataIntegrityAggregation(
        "VERIFIED_WITH_" + "_AND_".join(warnings), tuple(warnings), (),
    )


def reconstruct_data_integrity(
    *,
    scalars: Mapping[str, object],
    consistency: Mapping[str, LegacyConsistencyResult],
    split_integrity_status: str,
    current_yoy_crosses_split: bool | None,
    shares_quality: str,
) -> IntegrityReconstruction:
    quality = classify_data_quality(
        latest_eps_yoy_pct=scalars.get("latest_eps_yoy_pct"),
        previous_eps_yoy_pct=scalars.get("previous_eps_yoy_pct"),
        eps_acceleration_pp=scalars.get("eps_acceleration_pp"),
        latest_revenue_yoy_pct=scalars.get("latest_revenue_yoy_pct"),
        previous_revenue_yoy_pct=scalars.get("previous_revenue_yoy_pct"),
        revenue_acceleration_pp=scalars.get("revenue_acceleration_pp"),
        current_yoy_crosses_split=current_yoy_crosses_split,
        split_integrity_status=split_integrity_status,
    )
    aggregate = aggregate_data_integrity(
        quality, split_integrity_status, consistency, shares_quality,
    )
    diagnostics = list(aggregate.diagnostics)
    for metric, result in consistency.items():
        if result.reasons:
            diagnostics.extend(f"{metric}:{reason}" for reason in result.reasons)
    return IntegrityReconstruction(
        aggregate.data_integrity,
        quality,
        split_integrity_status,
        current_yoy_crosses_split,
        dict(consistency),
        shares_quality,
        aggregate.warnings,
        tuple(diagnostics),
    )


def _object_value(value: object, name: str, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _six_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 6)
    except ValueError:
        return day.replace(year=day.year - 6, day=28)


def _compare_resolved_sec_consistency(
    resolution: object,
    yahoo_rows: Sequence[Mapping[str, object]],
    *,
    metric: str,
) -> LegacyConsistencyResult:
    """Compare Yahoo only with the authoritative SEC series already resolved."""
    ambiguous_periods = tuple(_object_value(resolution, "ambiguous_periods", ()))
    if ambiguous_periods:
        return LegacyConsistencyResult(
            "AMBIGUOUS",
            None,
            None,
            0,
            reasons=("SEC_LATEST_FILING_AMBIGUOUS",),
        )
    resolved_metric = _object_value(resolution, "metric")
    selected_tag = _object_value(resolution, "selected_tag")
    raw_ids = _object_value(resolution, "raw_ids", {})
    sec_rows = [
        {
            "source": "SEC",
            "source_variant": "sec.company_facts",
            "metric": resolved_metric,
            "source_metric_name": selected_tag,
            "source_period_end": period,
            "series_date": period,
            "value": value,
            "raw_id": raw_ids.get(period),
        }
        for period, value in _object_value(resolution, "values", {}).items()
    ]
    return compare_legacy_source_consistency(
        sec_rows,
        yahoo_rows,
        metric=metric,
        candidate_order_demonstrated=bool(
            _object_value(resolution, "candidate_order_demonstrated", False)
        ),
    )


def reconstruct_persisted_integrity(
    normalized_evidence: Sequence[Mapping[str, object]],
    captures: Iterable[object],
    events_by_capture_id: Mapping[object, Sequence[object]],
    *,
    as_of: datetime,
    scalars: Mapping[str, object],
) -> IntegrityReconstruction:
    """Compose normalized v2 and persisted corporate-action evidence."""
    from database.split_integrity import (
        current_yoy_crosses_split,
        reconstruct_split_integrity,
        select_latest_persisted_capture_as_of,
        resolve_legacy_sec_metric,
    )

    visible = [
        row for row in normalized_evidence
        if _object_value(row, "observed_at") is not None
        and _object_value(row, "observed_at") <= as_of
    ]
    window_start = _six_years_before(as_of.date())
    resolved = {
        metric: resolve_legacy_sec_metric(
            visible, metric, as_of, window_start,
        )
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")
    }
    yahoo_metric_rows = {
        "EPS": [
            row for row in visible
            if _object_value(row, "source") == "YAHOO"
            and _object_value(row, "metric") in {"EPS_DILUTED", "EPS_BASIC", "EPS_UNSPECIFIED"}
        ],
        "REVENUE": [
            row for row in visible
            if _object_value(row, "source") == "YAHOO"
            and _object_value(row, "metric") == "REVENUE"
        ],
        "NET_INCOME": [
            row for row in visible
            if _object_value(row, "source") == "YAHOO"
            and _object_value(row, "metric") == "NET_INCOME"
        ],
    }
    consistency = {
        "EPS": _compare_resolved_sec_consistency(
            resolved["EPS_DILUTED"], yahoo_metric_rows["EPS"], metric="EPS",
        ),
        "REVENUE": _compare_resolved_sec_consistency(
            resolved["REVENUE"], yahoo_metric_rows["REVENUE"], metric="REVENUE",
        ),
        "NET_INCOME": _compare_resolved_sec_consistency(
            resolved["NET_INCOME"], yahoo_metric_rows["NET_INCOME"], metric="NET_INCOME",
        ),
    }
    shares = resolved["DILUTED_SHARES"]
    shares_quality = shares.shares_quality
    selected_capture = select_latest_persisted_capture_as_of(captures, as_of)
    split_result = reconstruct_split_integrity(
        captures,
        events_by_capture_id,
        as_of,
        resolved["EPS_DILUTED"].values,
        resolved["NET_INCOME"].values,
        resolved["DILUTED_SHARES"].values,
        shares_quality,
        candidate_order_demonstrated=shares.candidate_order_demonstrated,
    )
    event_dates = []
    if selected_capture is not None:
        selected_id = _object_value(selected_capture, "id")
        event_dates = [
            _object_value(event, "event_date")
            for event in events_by_capture_id.get(selected_id, ())
            if _object_value(event, "event_date") is not None
        ]
    current_cross = current_yoy_crosses_split(
        event_dates,
        scalars.get("previous_eps_period"),
        scalars.get("latest_eps_period"),
        _object_value(selected_capture, "acquisition_status") if selected_capture else "NONE",
        _object_value(selected_capture, "completeness_status") if selected_capture else "NONE",
    )
    return reconstruct_data_integrity(
        scalars=scalars,
        consistency=consistency,
        split_integrity_status=split_result.status,
        current_yoy_crosses_split=current_cross,
        shares_quality=shares_quality,
    )


_LOAD_SQL = f"""
SELECT n.{", n.".join(_NORMALIZED_COLUMNS)},
       r.source_record_id AS raw_source_record_id,
       r.source_payload ->> 'accn' AS raw_payload_accn
FROM fundamentals_normalized AS n
LEFT JOIN fundamentals_raw AS r ON r.id = n.raw_id
WHERE n.company_id = %s
  AND n.observed_at <= %s
ORDER BY n.metric, n.source_period_end, n.source_variant, n.observed_at, n.id
"""


def _expose_source_identity(item: dict) -> None:
    """Expose only raw-backed identity that passes its source contract."""
    if "raw_source_record_id" not in item and "raw_payload_accn" not in item:
        return
    raw_id = item.pop("raw_source_record_id", None)
    payload_accn = item.pop("raw_payload_accn", None)
    source = item.get("source")
    variant = item.get("source_variant")
    if source == "SEC" and variant == "sec.company_facts":
        item["source_record_id"] = None
        item["source_identity_type"] = None
        if not raw_id:
            item["source_identity_error"] = "SEC_ACCESSION_MISSING"
        elif not payload_accn:
            item["source_identity_error"] = "SEC_ACCESSION_PAYLOAD_MISSING"
        elif raw_id != payload_accn:
            item["source_identity_error"] = "SEC_ACCESSION_MISMATCH"
        else:
            item["source_record_id"] = raw_id
            item["source_identity_type"] = "SEC_ACCESSION"
        return
    if source == "YAHOO" and raw_id:
        item["source_record_id"] = raw_id
        item["source_identity_type"] = "YAHOO_SOURCE_RECORD_ID"


def load_normalized_evidence(
    company_id: int,
    as_of: datetime,
    connection_factory: Callable[[], object] | None = None,
) -> tuple[dict, ...]:
    """Load only normalized evidence visible at ``as_of`` with one SELECT."""
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    connect = connection_factory
    if connect is None:
        from database.normalized_fundamentals import get_connection
        connect = get_connection
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(_LOAD_SQL, (company_id, as_of))
            columns = [description.name for description in cursor.description]
            visible = []
            for row in cursor.fetchall():
                item = dict(zip(columns, row))
                observed_at = item.get("observed_at")
                if observed_at is not None and observed_at > as_of:
                    continue
                _expose_source_identity(item)
                visible.append(item)
            return tuple(visible)


__all__ = [
    "LEGACY_CONSISTENCY_VERSION",
    "LegacyConsistencyResult",
    "ConsistencyMatch",
    "DataIntegrityAggregation",
    "IntegrityReconstruction",
    "compare_legacy_source_consistency",
    "classify_data_quality",
    "aggregate_data_integrity",
    "reconstruct_data_integrity",
    "reconstruct_persisted_integrity",
    "load_normalized_evidence",
    "VALID_DATA_INTEGRITY_STATUSES",
]
