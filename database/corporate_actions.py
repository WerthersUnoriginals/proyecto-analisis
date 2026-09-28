"""Append-only evidence contract for point-in-time corporate-action captures."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Sequence
from uuid import UUID

try:
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # Pure/offline tests do not require the PostgreSQL driver.
    dict_row = None

    class Jsonb:  # pragma: no cover
        def __init__(self, obj):
            self.obj = obj


FINGERPRINT_VERSION = "corporate-action-response-sha256-v2"
CAPTURE_CONTRACT_VERSION = "legacy-split-capture-v1"
PROVIDER = "YAHOO_FINANCE"
SOURCE_VARIANT = "yfinance.splits"

_ACQUISITION_STATUSES = {"SUCCESS", "PARTIAL", "FAILED", "UNKNOWN"}
_COMPLETENESS_STATUSES = {"COMPLETE", "INCOMPLETE", "UNKNOWN"}
_SAFE_ERROR_KEYS = {
    "operation",
    "exception_type",
    "retryable",
    "http_status",
    "provider_code",
}
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class IdempotencyConflict(RuntimeError):
    """A capture UID was reused for different immutable evidence."""

    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True)
class CaptureEvidence:
    capture_uid: UUID
    company_id: int
    provider: str
    source_variant: str
    capture_contract_version: str
    requested_window_start_at: datetime
    requested_window_end_at: datetime | None
    capture_started_at: datetime
    capture_completed_at: datetime
    observed_at: datetime
    source_available_at: datetime | None
    acquisition_status: str
    completeness_status: str
    provider_capture_id: str | None
    provider_revision_id: str | None
    provider_metadata: Mapping[str, object]
    error_code: str | None
    error_metadata: Mapping[str, object]
    response_fingerprint_version: str | None
    response_fingerprint: str | None
    id: int | None = None


@dataclass(frozen=True)
class CorporateActionEvent:
    event_ordinal: int
    action_type: str
    event_date: date
    split_ratio: Decimal
    provider_event_id: str | None
    provider_revision_id: str | None
    provider_metadata: Mapping[str, object]
    id: int | None = None
    capture_id: int | None = None


def get_connection():
    from database.db import get_connection as connect

    return connect()


def sanitize_error_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """Keep only non-secret diagnostic fields; never persist exception messages."""

    if not metadata:
        return {}
    sanitized = {}
    for key in _SAFE_ERROR_KEYS:
        if key not in metadata:
            continue
        value = metadata[key]
        if isinstance(value, str):
            value = value[:160]
        elif not isinstance(value, (bool, int, type(None))):
            continue
        sanitized[key] = value
    return sanitized


def _decimal_text(value: object) -> str:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("numeric metadata must be finite")
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _six_years_before(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 6)
    except ValueError:  # 29 February follows PostgreSQL interval calendar semantics.
        return value.replace(year=value.year - 6, day=28)


def _canonical_metadata(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("numeric metadata must be finite")
        return {"$type": "number", "value": _decimal_text(value)}
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("metadata keys must be strings")
        return {key: _canonical_metadata(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_metadata(item) for item in value]
    raise ValueError(f"metadata value is not JSON-compatible: {type(value).__name__}")


def _fingerprint_material(capture: CaptureEvidence, events: Iterable[CorporateActionEvent]) -> dict:
    ordered = sorted(events, key=lambda item: item.event_ordinal)
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "company_id": capture.company_id,
        "provider": capture.provider,
        "source_variant": capture.source_variant,
        "capture_contract_version": capture.capture_contract_version,
        "requested_window_start_at": _utc_text(capture.requested_window_start_at),
        "requested_window_end_at": _utc_text(capture.requested_window_end_at),
        "source_available_at": _utc_text(capture.source_available_at),
        "acquisition_status": capture.acquisition_status,
        "completeness_status": capture.completeness_status,
        "provider_capture_id": capture.provider_capture_id,
        "provider_revision_id": capture.provider_revision_id,
        "provider_metadata": _canonical_metadata(capture.provider_metadata),
        "error_code": capture.error_code,
        "error_metadata": _canonical_metadata(capture.error_metadata),
        "events": [
            {
                "event_ordinal": item.event_ordinal,
                "action_type": item.action_type,
                "event_date": item.event_date.isoformat(),
                "split_ratio": _decimal_text(item.split_ratio),
                "provider_event_id": item.provider_event_id,
                "provider_revision_id": item.provider_revision_id,
                "provider_metadata": _canonical_metadata(item.provider_metadata),
            }
            for item in ordered
        ],
    }


def _canonical_json(material: object) -> str:
    return json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def build_response_fingerprint(capture: CaptureEvidence, events: Iterable[CorporateActionEvent]) -> str:
    """Return the documented SHA-256 v2 fingerprint of persisted response evidence."""

    payload = _canonical_json(_fingerprint_material(capture, events)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_capture_bundle(capture: CaptureEvidence, events: Sequence[CorporateActionEvent]) -> None:
    for value in (
        capture.requested_window_start_at,
        capture.requested_window_end_at,
        capture.capture_started_at,
        capture.capture_completed_at,
        capture.observed_at,
        capture.source_available_at,
    ):
        _utc_text(value)
    if capture.provider != PROVIDER or capture.source_variant != SOURCE_VARIANT:
        raise ValueError("unsupported provider/source_variant")
    if capture.capture_contract_version != CAPTURE_CONTRACT_VERSION:
        raise ValueError("unsupported capture contract")
    if capture.acquisition_status not in _ACQUISITION_STATUSES:
        raise ValueError("unsupported acquisition status")
    if capture.completeness_status not in _COMPLETENESS_STATUSES:
        raise ValueError("unsupported completeness status")
    if capture.capture_started_at > capture.capture_completed_at:
        raise ValueError("capture timestamps are out of order")
    if capture.observed_at != capture.capture_completed_at:
        raise ValueError("observed_at must equal capture_completed_at")
    if capture.source_available_at and capture.source_available_at > capture.observed_at:
        raise ValueError("source availability cannot be after observation")
    if capture.requested_window_end_at is not None:
        raise ValueError("legacy-split-capture-v1 requires an open upper window")
    if capture.requested_window_start_at != _six_years_before(capture.capture_started_at):
        raise ValueError("legacy-split-capture-v1 requires the exact six-year lower window")
    if [item.event_ordinal for item in sorted(events, key=lambda item: item.event_ordinal)] != list(range(len(events))):
        raise ValueError("event ordinals must be contiguous from zero")
    for item in events:
        if item.action_type != "STOCK_SPLIT" or not item.split_ratio.is_finite() or item.split_ratio <= 0:
            raise ValueError("invalid split event")
        if item.event_date < capture.requested_window_start_at.astimezone(timezone.utc).date():
            raise ValueError("event is outside requested window")
        if capture.requested_window_end_at and item.event_date > capture.requested_window_end_at.astimezone(timezone.utc).date():
            raise ValueError("event is outside requested window")
        _canonical_metadata(item.provider_metadata)
    _canonical_metadata(capture.provider_metadata)
    _canonical_metadata(capture.error_metadata)
    if dict(capture.error_metadata) != sanitize_error_metadata(capture.error_metadata):
        raise ValueError("error_metadata must be sanitized before persistence")
    if capture.error_code is not None and not _ERROR_CODE_PATTERN.fullmatch(capture.error_code):
        raise ValueError("error_code must be a bounded symbolic code")
    if capture.acquisition_status == "SUCCESS":
        if capture.completeness_status != "COMPLETE" or capture.error_code is not None:
            raise ValueError("SUCCESS requires COMPLETE and no error")
    elif capture.acquisition_status == "PARTIAL":
        if capture.completeness_status != "INCOMPLETE":
            raise ValueError("PARTIAL requires INCOMPLETE")
    else:
        if events:
            raise ValueError("FAILED/UNKNOWN captures cannot contain events")
        if not capture.error_code:
            raise ValueError("FAILED/UNKNOWN captures require an error code")
        if capture.completeness_status not in {"INCOMPLETE", "UNKNOWN"}:
            raise ValueError("FAILED/UNKNOWN cannot be COMPLETE")
    has_response = capture.acquisition_status in {"SUCCESS", "PARTIAL"}
    if has_response:
        if capture.response_fingerprint_version != FINGERPRINT_VERSION:
            raise ValueError("fingerprint version is required")
        expected = build_response_fingerprint(capture, events)
        if capture.response_fingerprint != expected:
            raise ValueError("response fingerprint does not match persisted evidence")
    elif capture.response_fingerprint_version is not None or capture.response_fingerprint is not None:
        raise ValueError("failed/unknown captures cannot claim a response fingerprint")


def _bundle_material(capture: CaptureEvidence, events: Sequence[CorporateActionEvent]) -> str:
    header = {
        field.name: getattr(capture, field.name)
        for field in fields(CaptureEvidence)
        if field.name != "id"
    }
    for key in tuple(header):
        value = header[key]
        if isinstance(value, datetime):
            header[key] = _utc_text(value)
        elif isinstance(value, UUID):
            header[key] = str(value)
        elif isinstance(value, Mapping):
            header[key] = _canonical_metadata(value)
    return _canonical_json({"capture": header, "response": _fingerprint_material(capture, events)})


def decide_idempotent_write(
    proposed_capture: CaptureEvidence,
    proposed_events: Sequence[CorporateActionEvent],
    existing_capture: CaptureEvidence | None,
    existing_events: Sequence[CorporateActionEvent],
) -> str:
    validate_capture_bundle(proposed_capture, proposed_events)
    if existing_capture is None:
        return "INSERT"
    validate_capture_bundle(existing_capture, existing_events)
    if proposed_capture.capture_uid != existing_capture.capture_uid:
        raise ValueError("existing capture must have the proposed capture_uid")
    if _bundle_material(proposed_capture, proposed_events) != _bundle_material(existing_capture, existing_events):
        raise IdempotencyConflict()
    return "RETURN_EXISTING"


def select_latest_capture_as_of(
    captures: Iterable[CaptureEvidence], as_of: datetime
) -> CaptureEvidence | None:
    _utc_text(as_of)
    eligible = [item for item in captures if item.observed_at <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.observed_at, item.id or 0))


_CAPTURE_COLUMNS = tuple(field.name for field in fields(CaptureEvidence))
_EVENT_COLUMNS = tuple(field.name for field in fields(CorporateActionEvent))

LOAD_CAPTURE_BY_UID_SQL = f"""
    SELECT {", ".join(_CAPTURE_COLUMNS)}
    FROM public.corporate_action_captures
    WHERE capture_uid = %s;
"""

LOAD_LATEST_CAPTURE_SQL = f"""
    SELECT {", ".join(_CAPTURE_COLUMNS)}
    FROM public.corporate_action_captures
    WHERE company_id = %s
      AND source_variant = %s
      AND capture_contract_version = %s
      AND observed_at <= %s
    ORDER BY observed_at DESC, id DESC
    LIMIT 1;
"""

LOAD_EVENTS_SQL = f"""
    SELECT {", ".join(_EVENT_COLUMNS)}
    FROM public.corporate_action_events
    WHERE capture_id = %s
    ORDER BY event_ordinal;
"""

_INSERT_CAPTURE_COLUMNS = tuple(name for name in _CAPTURE_COLUMNS if name != "id")
INSERT_CAPTURE_SQL = f"""
    INSERT INTO public.corporate_action_captures ({", ".join(_INSERT_CAPTURE_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(_INSERT_CAPTURE_COLUMNS))})
    ON CONFLICT (capture_uid) DO NOTHING
    RETURNING id;
"""

_INSERT_EVENT_COLUMNS = (
    "capture_id",
    "event_ordinal",
    "action_type",
    "event_date",
    "split_ratio",
    "provider_event_id",
    "provider_revision_id",
    "provider_metadata",
)
INSERT_EVENT_SQL = f"""
    INSERT INTO public.corporate_action_events ({", ".join(_INSERT_EVENT_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(_INSERT_EVENT_COLUMNS))});
"""


def _capture_from_row(row: Mapping[str, object]) -> CaptureEvidence:
    values = {name: row[name] for name in _CAPTURE_COLUMNS}
    if not isinstance(values["capture_uid"], UUID):
        values["capture_uid"] = UUID(str(values["capture_uid"]))
    return CaptureEvidence(**values)


def _event_from_row(row: Mapping[str, object]) -> CorporateActionEvent:
    values = {name: row[name] for name in _EVENT_COLUMNS}
    values["split_ratio"] = Decimal(str(values["split_ratio"]))
    return CorporateActionEvent(**values)


def _capture_params(capture: CaptureEvidence) -> tuple:
    values = []
    for name in _INSERT_CAPTURE_COLUMNS:
        value = getattr(capture, name)
        if name in {"provider_metadata", "error_metadata"}:
            value = Jsonb(_canonical_metadata(value))
        values.append(value)
    return tuple(values)


def _event_params(capture_id: int, event: CorporateActionEvent) -> tuple:
    return (
        capture_id,
        event.event_ordinal,
        event.action_type,
        event.event_date,
        event.split_ratio,
        event.provider_event_id,
        event.provider_revision_id,
        Jsonb(_canonical_metadata(event.provider_metadata)),
    )


def _load_events_with_cursor(cursor, capture_id: int) -> tuple[CorporateActionEvent, ...]:
    cursor.execute(LOAD_EVENTS_SQL, (capture_id,))
    return tuple(_event_from_row(row) for row in cursor.fetchall())


def record_capture_bundle(
    capture: CaptureEvidence,
    events: Sequence[CorporateActionEvent],
    *,
    connection_factory=None,
) -> tuple[CaptureEvidence, tuple[CorporateActionEvent, ...]]:
    """Insert one immutable bundle, or return its exact idempotent predecessor."""

    validate_capture_bundle(capture, events)
    connect = connection_factory or get_connection
    with connect() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(LOAD_CAPTURE_BY_UID_SQL, (capture.capture_uid,))
            existing_row = cursor.fetchone()
            if existing_row is not None:
                existing = _capture_from_row(existing_row)
                existing_events = _load_events_with_cursor(cursor, existing.id)
                decide_idempotent_write(capture, events, existing, existing_events)
                return existing, existing_events

            cursor.execute(INSERT_CAPTURE_SQL, _capture_params(capture))
            inserted = cursor.fetchone()
            if inserted is None:
                # A concurrent transaction won the unique capture_uid race.
                cursor.execute(LOAD_CAPTURE_BY_UID_SQL, (capture.capture_uid,))
                existing = _capture_from_row(cursor.fetchone())
                existing_events = _load_events_with_cursor(cursor, existing.id)
                decide_idempotent_write(capture, events, existing, existing_events)
                return existing, existing_events

            capture_id = int(inserted["id"] if isinstance(inserted, Mapping) else inserted[0])
            if events:
                cursor.executemany(
                    INSERT_EVENT_SQL,
                    [_event_params(capture_id, item) for item in sorted(events, key=lambda value: value.event_ordinal)],
                )
            stored_capture = CaptureEvidence(**{**capture.__dict__, "id": capture_id})
            stored_events = tuple(
                CorporateActionEvent(**{**item.__dict__, "capture_id": capture_id})
                for item in sorted(events, key=lambda value: value.event_ordinal)
            )
            return stored_capture, stored_events


def load_latest_capture_as_of(
    company_id: int,
    as_of: datetime,
    *,
    source_variant: str = SOURCE_VARIANT,
    capture_contract_version: str = CAPTURE_CONTRACT_VERSION,
    connection_factory=None,
) -> tuple[CaptureEvidence | None, tuple[CorporateActionEvent, ...]]:
    """Read the latest attempt as-of without falling back based on its status."""

    _utc_text(as_of)
    connect = connection_factory or get_connection
    with connect() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                LOAD_LATEST_CAPTURE_SQL,
                (company_id, source_variant, capture_contract_version, as_of),
            )
            row = cursor.fetchone()
            if row is None:
                return None, ()
            capture = _capture_from_row(row)
            events = _load_events_with_cursor(cursor, capture.id)
            validate_capture_bundle(capture, events)
            return capture, events


def load_capture_by_uid(
    capture_uid: UUID,
    *,
    connection_factory=None,
) -> tuple[CaptureEvidence | None, tuple[CorporateActionEvent, ...]]:
    """Preflight a durable attempt UID before any provider retry."""

    connect = connection_factory or get_connection
    with connect() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(LOAD_CAPTURE_BY_UID_SQL, (capture_uid,))
            row = cursor.fetchone()
            if row is None:
                return None, ()
            capture = _capture_from_row(row)
            events = _load_events_with_cursor(cursor, capture.id)
            validate_capture_bundle(capture, events)
            return capture, events
