"""Persistencia append-only de observaciones fundamentales normalizadas."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Sequence

try:
    from psycopg.types.json import Jsonb
except ImportError:  # Permite validar la lógica pura sin el driver PostgreSQL instalado.
    class Jsonb:  # pragma: no cover - el driver real aporta esta clase en producción.
        def __init__(self, obj):
            self.obj = obj


def get_connection():
    """Importa el driver sólo al acceder a PostgreSQL."""

    from database.db import get_connection as connect

    return connect()


SEC_VARIANT = "sec.company_facts"
SEC_NORMALIZER_VERSION = "sec-normalized-v1"
YAHOO_NORMALIZER_VERSION = "yahoo-normalized-v1"
SEC_NORMALIZER_V2 = "sec-normalized-v2"
YAHOO_NORMALIZER_V2 = "yahoo-normalized-v2"

_METRIC_UNITS = {
    "EPS_DILUTED": "USD/shares",
    "EPS_BASIC": "USD/shares",
    "EPS_UNSPECIFIED": "USD/shares",
    "REVENUE": "USD",
    "NET_INCOME": "USD",
    "DILUTED_SHARES": "shares",
}
_QUALITY_STATUSES = {"OK", "REVIEW_REQUIRED", "REVIEW_REQUIRED_HIGH", "REJECTED"}
_ALIGNMENT_METHODS = {
    "EXACT", "FISCAL_METADATA", "SEC_CALENDAR", "NEAREST_35D", "SOURCE_ONLY", "UNRESOLVED"
}


@dataclass(frozen=True)
class NormalizedObservation:
    company_id: int
    metric: str
    source: str
    source_variant: str
    observation_kind: str
    value: Decimal
    unit: str
    currency: str | None
    source_period_start: date | None
    source_period_end: date
    canonical_period_end: date | None
    series_date: date
    fiscal_year: int | None
    fiscal_quarter: int | None
    filed_date: date | None
    source_available_at: datetime | None
    observed_at: datetime
    raw_id: int
    normalizer_version: str
    intrinsic_quality_status: str
    intrinsic_quality_reasons: tuple[str, ...]
    selection_eligibility: str
    alignment_method: str
    alignment_days: int | None
    alignment_reference_id: int | None
    source_metric_name: str | None = None
    source_unit: str | None = None
    source_scale_factor: Decimal | None = None
    id: int | None = None

    def __post_init__(self):
        if self.metric not in _METRIC_UNITS:
            raise ValueError(f"Métrica no admitida: {self.metric}")
        if self.unit != _METRIC_UNITS[self.metric]:
            raise ValueError(f"Unidad no compatible con {self.metric}: {self.unit}")
        if self.source not in {"SEC", "YAHOO", "DERIVED"}:
            raise ValueError(f"Fuente no admitida: {self.source}")
        if self.source == "SEC" and not self.source_variant.startswith("sec."):
            raise ValueError("La variante SEC debe empezar por sec.")
        if self.source == "YAHOO" and not (
            self.source_variant.startswith("yahoo.") or self.source_variant.startswith("yfinance.")
        ):
            raise ValueError("La variante Yahoo debe empezar por yahoo. o yfinance.")
        if self.source == "DERIVED" and not self.source_variant.startswith("derived."):
            raise ValueError("La variante DERIVED debe empezar por derived.")
        if self.observation_kind not in {"REPORTED", "DERIVED"}:
            raise ValueError("Clase de observación no admitida")
        if self.source == "SEC" and self.observation_kind != "REPORTED":
            raise ValueError("SEC sólo admite observaciones REPORTED")
        if (
            self.source == "SEC"
            and self.selection_eligibility == "ELIGIBLE"
            and self.filed_date is None
        ):
            raise ValueError("SEC elegible requiere filed_date")
        if self.source == "YAHOO" and (self.observation_kind != "REPORTED" or self.filed_date is not None):
            raise ValueError("Yahoo debe ser REPORTED y no tener filed_date")
        if self.source == "DERIVED" and self.observation_kind != "DERIVED":
            raise ValueError("DERIVED requiere observation_kind=DERIVED")
        if self.source == "DERIVED" and self.selection_eligibility != "INELIGIBLE":
            raise ValueError("DERIVED requiere selection_eligibility=INELIGIBLE")
        if self.source_period_start and self.source_period_start > self.source_period_end:
            raise ValueError("source_period_start no puede ser posterior a source_period_end")
        if self.series_date != self.source_period_end:
            raise ValueError("series_date debe conservar source_period_end")
        if self.fiscal_quarter is not None and self.fiscal_quarter not in {1, 2, 3, 4}:
            raise ValueError("fiscal_quarter debe estar entre 1 y 4")
        if self.intrinsic_quality_status not in _QUALITY_STATUSES:
            raise ValueError("Estado de calidad intrínseca no admitido")
        if self.selection_eligibility not in {"ELIGIBLE", "INELIGIBLE"}:
            raise ValueError("Estado de elegibilidad no admitido")
        if self.intrinsic_quality_status != "OK" and self.selection_eligibility != "INELIGIBLE":
            raise ValueError("Una observación revisable o rechazada es inelegible")
        if self.alignment_method not in _ALIGNMENT_METHODS:
            raise ValueError("Método de alineación no admitido")
        if self.canonical_period_end is None:
            if self.alignment_days is not None:
                raise ValueError("alignment_days requiere canonical_period_end")
        elif self.alignment_days != (self.source_period_end - self.canonical_period_end).days:
            raise ValueError("alignment_days no coincide con las fechas de periodo")
        if self.alignment_method == "EXACT" and (
            self.canonical_period_end is None or self.alignment_days != 0
        ):
            raise ValueError("La alineación EXACT requiere fechas idénticas")
        if self.alignment_method == "UNRESOLVED" and (
            self.canonical_period_end is not None
            or self.intrinsic_quality_status == "OK"
            or self.selection_eligibility != "INELIGIBLE"
        ):
            raise ValueError("UNRESOLVED requiere revisión e inelegibilidad")
        if self.normalizer_version in {SEC_NORMALIZER_V2, YAHOO_NORMALIZER_V2}:
            if not self.source_metric_name or not self.source_metric_name.strip():
                raise ValueError("v2 requiere source_metric_name")
            if not self.source_unit or not self.source_unit.strip():
                raise ValueError("v2 requiere source_unit")
            if self.source_scale_factor is None or self.source_scale_factor <= 0:
                raise ValueError("v2 requiere source_scale_factor positivo")
        if self.metric == "EPS_UNSPECIFIED" and not (
            self.source == "YAHOO"
            and self.normalizer_version == YAHOO_NORMALIZER_V2
            and self.source_metric_name == "legacy.unknown"
            and self.intrinsic_quality_status == "REVIEW_REQUIRED"
            and self.selection_eligibility == "INELIGIBLE"
        ):
            raise ValueError("EPS_UNSPECIFIED debe ser Yahoo legacy, revisable e inelegible")


@dataclass(frozen=True)
class FiscalIdentityResult:
    fiscal_year: int | None
    fiscal_quarter: int | None
    canonical_period_end: date | None
    alignment_method: str
    alignment_days: int | None
    alignment_reference_id: int | None
    reasons: tuple[str, ...] = ()


_NORMALIZED_COLUMNS = (
    "company_id", "metric", "source", "source_variant", "observation_kind", "value", "unit",
    "currency", "source_period_start", "source_period_end", "canonical_period_end", "series_date",
    "fiscal_year", "fiscal_quarter", "filed_date", "source_available_at", "observed_at", "raw_id",
    "normalizer_version", "intrinsic_quality_status", "intrinsic_quality_reasons",
    "selection_eligibility", "alignment_method", "alignment_days", "alignment_reference_id",
    "source_metric_name", "source_unit", "source_scale_factor",
)

INSERT_NORMALIZED_SQL = f"""
    INSERT INTO fundamentals_normalized ({", ".join(_NORMALIZED_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(_NORMALIZED_COLUMNS))})
    ON CONFLICT (raw_id, normalizer_version) DO NOTHING
    RETURNING id;
"""

FIND_NORMALIZED_SQL = f"""
    SELECT id, {", ".join(_NORMALIZED_COLUMNS)}
    FROM fundamentals_normalized
    WHERE raw_id = %s AND normalizer_version = %s;
"""

LOAD_RAW_SQL = """
    SELECT
        id, company_id, source,
        CASE WHEN source = 'SEC' THEN 'sec.company_facts' END AS source_variant,
        metric, period_start, period_end, filed_date,
        fiscal_year, fiscal_quarter, form_type, value, unit, currency, xbrl_tag,
        source_record_id, source_payload, fetched_at, created_at
    FROM fundamentals_raw
    WHERE company_id = %s
"""


def _sec_duration_is_quarterly(row: dict) -> bool:
    period_start = row.get("period_start")
    period_end = row.get("period_end")
    return (
        isinstance(period_start, date)
        and isinstance(period_end, date)
        and 70 <= (period_end - period_start).days <= 110
    )


def _original_sec_fiscal_identity(rows: Iterable[dict]) -> dict:
    """Replica la identidad fiscal original sin alterar los valores raw."""

    rows = list(rows)
    candidates = [row for row in rows if row.get("fiscal_year") is not None]
    if not candidates:
        candidates = rows
    candidates.sort(
        key=lambda row: (
            row.get("filed_date") is None,
            row.get("filed_date") or date.max,
            row["id"],
        )
    )
    original = candidates[0] if candidates else None
    if original is None:
        return {"fiscal_year": None, "fiscal_quarter": None}

    fiscal_quarter = original.get("fiscal_quarter")
    if (
        fiscal_quarter is None
        and original.get("form_type") in {"10-K", "10-K/A"}
        and _sec_duration_is_quarterly(original)
    ):
        fiscal_quarter = 4
    return {
        "fiscal_year": original.get("fiscal_year"),
        "fiscal_quarter": fiscal_quarter,
    }


def normalize_sec_raw_row(row: dict, fiscal_identity: dict) -> NormalizedObservation:
    """Normaliza una evidencia SEC conservando sus fechas y valor publicados."""

    if row.get("source") != "SEC":
        raise ValueError("normalize_sec_raw_row sólo acepta evidencia SEC")
    if row.get("source_variant") != SEC_VARIANT:
        raise ValueError("source_variant SEC no coincide con el contrato sec.company_facts")
    observed_at = row.get("fetched_at")
    if not isinstance(observed_at, datetime):
        raise ValueError("Una fila raw SEC requiere fetched_at para observed_at")

    reasons = []
    if row.get("filed_date") is None:
        reasons.append("MISSING_FILED_DATE")
    if not _sec_duration_is_quarterly(row):
        reasons.append("NON_QUARTERLY_PERIOD")
    fiscal_year = fiscal_identity.get("fiscal_year")
    fiscal_quarter = fiscal_identity.get("fiscal_quarter")
    if fiscal_year is None:
        reasons.append("MISSING_FISCAL_YEAR")
    if fiscal_quarter is None:
        reasons.append("MISSING_FISCAL_QUARTER")
    quality = "OK" if not reasons else "REVIEW_REQUIRED"
    eligibility = "ELIGIBLE" if quality == "OK" else "INELIGIBLE"
    period_end = row["period_end"]

    return NormalizedObservation(
        company_id=row["company_id"],
        metric=row["metric"],
        source="SEC",
        source_variant=SEC_VARIANT,
        observation_kind="REPORTED",
        value=Decimal(str(row["value"])),
        unit=row["unit"],
        currency=row.get("currency"),
        source_period_start=row.get("period_start"),
        source_period_end=period_end,
        canonical_period_end=period_end,
        series_date=period_end,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        filed_date=row.get("filed_date"),
        source_available_at=None,
        observed_at=observed_at,
        raw_id=row["id"],
        normalizer_version=SEC_NORMALIZER_VERSION,
        intrinsic_quality_status=quality,
        intrinsic_quality_reasons=tuple(reasons),
        selection_eligibility=eligibility,
        alignment_method="EXACT",
        alignment_days=0,
        alignment_reference_id=None,
    )


def normalize_sec_raw_row_v2(row: dict, fiscal_identity: dict) -> NormalizedObservation:
    """Añade semántica demostrable por el tag XBRL ya guardado en raw."""

    item = normalize_sec_raw_row(row, fiscal_identity)
    source_metric_name = row.get("xbrl_tag")
    if not source_metric_name:
        raise ValueError("SEC v2 requiere el tag XBRL original")
    metric = item.metric
    if metric in {"EPS_DILUTED", "EPS_BASIC"}:
        lowered = source_metric_name.lower()
        if "diluted" in lowered:
            metric = "EPS_DILUTED"
        elif "basic" in lowered:
            metric = "EPS_BASIC"
        else:
            raise ValueError("El tag SEC no demuestra semántica EPS Basic/Diluted")
    return replace(
        item,
        metric=metric,
        normalizer_version=SEC_NORMALIZER_V2,
        source_metric_name=source_metric_name,
        source_unit=row["unit"],
        source_scale_factor=Decimal("1"),
    )


def _fiscal_index(fiscal_year: int, fiscal_quarter: int) -> int:
    return fiscal_year * 4 + fiscal_quarter - 1


def _fiscal_identity(index: int) -> tuple[int, int]:
    return index // 4, index % 4 + 1


def _yahoo_calendar_rows(
    row: dict, sec_calendar: Sequence[NormalizedObservation]
) -> list[NormalizedObservation]:
    unique = {}
    for item in sec_calendar:
        if (
            item.source != "SEC"
            or item.company_id != row["company_id"]
            or item.metric != row["metric"]
            or item.fiscal_year is None
            or item.fiscal_quarter is None
            or item.canonical_period_end is None
        ):
            continue
        key = (item.fiscal_year, item.fiscal_quarter, item.canonical_period_end)
        current = unique.get(key)
        if current is None or item.raw_id < current.raw_id:
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (item.canonical_period_end, item.raw_id))


def _unresolved_yahoo_identity(*reasons: str) -> FiscalIdentityResult:
    return FiscalIdentityResult(
        fiscal_year=None,
        fiscal_quarter=None,
        canonical_period_end=None,
        alignment_method="UNRESOLVED",
        alignment_days=None,
        alignment_reference_id=None,
        reasons=tuple(reasons) or ("YAHOO_FISCAL_IDENTITY_UNRESOLVED_V1",),
    )


def resolve_yahoo_fiscal_identity(
    row: dict, sec_calendar: Sequence[NormalizedObservation]
) -> FiscalIdentityResult:
    """Resuelve identidad fiscal sin tratar la proximidad como evidencia suficiente."""

    source_date = row["period_end"]
    calendar = _yahoo_calendar_rows(row, sec_calendar)
    metadata = (row.get("fiscal_year"), row.get("fiscal_quarter"))
    if (metadata[0] is None) != (metadata[1] is None):
        return _unresolved_yahoo_identity("INCOMPLETE_FISCAL_METADATA_V1")

    nearby = [
        item for item in calendar
        if abs((source_date - item.canonical_period_end).days) <= 35
    ]
    reference = None
    tied = False
    if nearby:
        nearest_distance = min(abs((source_date - item.canonical_period_end).days) for item in nearby)
        nearest = [
            item for item in nearby
            if abs((source_date - item.canonical_period_end).days) == nearest_distance
        ]
        nearest_identities = {
            (item.fiscal_year, item.fiscal_quarter, item.canonical_period_end)
            for item in nearest
        }
        tied = len(nearest_identities) > 1
        if not tied:
            reference = min(nearest, key=lambda item: item.raw_id)

    if tied:
        return _unresolved_yahoo_identity("AMBIGUOUS_FISCAL_IDENTITY_V1")

    before = [item for item in calendar if item.canonical_period_end < source_date]
    after = [item for item in calendar if item.canonical_period_end > source_date]
    inferred = None
    if reference is None and before and after:
        previous_date = max(item.canonical_period_end for item in before)
        next_date = min(item.canonical_period_end for item in after)
        previous = [item for item in before if item.canonical_period_end == previous_date]
        following = [item for item in after if item.canonical_period_end == next_date]
        previous_identities = {(item.fiscal_year, item.fiscal_quarter) for item in previous}
        following_identities = {(item.fiscal_year, item.fiscal_quarter) for item in following}
        if len(previous_identities) == 1 and len(following_identities) == 1:
            previous_identity = next(iter(previous_identities))
            following_identity = next(iter(following_identities))
            previous_index = _fiscal_index(*previous_identity)
            following_index = _fiscal_index(*following_identity)
            if following_index - previous_index == 2:
                inferred = _fiscal_identity(previous_index + 1)

    identity = metadata if metadata[0] is not None else None
    identity_method = "FISCAL_METADATA" if identity is not None else None
    if identity is None and inferred is not None:
        identity = inferred
        identity_method = "SEC_CALENDAR"

    if identity is None and reference is not None:
        reference_index = _fiscal_index(reference.fiscal_year, reference.fiscal_quarter)
        corroborated = False
        for item in calendar:
            if item.raw_id == reference.raw_id:
                continue
            fiscal_delta = _fiscal_index(item.fiscal_year, item.fiscal_quarter) - reference_index
            date_delta = (item.canonical_period_end - reference.canonical_period_end).days
            if (fiscal_delta == 1 and date_delta > 0) or (fiscal_delta == -1 and date_delta < 0):
                corroborated = True
                break
        if corroborated:
            identity = (reference.fiscal_year, reference.fiscal_quarter)

    if identity is None:
        reason = (
            "PROXIMITY_WITHOUT_FISCAL_IDENTITY_V1"
            if reference is not None
            else "YAHOO_FISCAL_IDENTITY_UNRESOLVED_V1"
        )
        return _unresolved_yahoo_identity(reason)

    if reference is not None and identity != (reference.fiscal_year, reference.fiscal_quarter):
        return _unresolved_yahoo_identity("CONTRADICTORY_FISCAL_IDENTITY_V1")

    if reference is not None:
        alignment_days = (source_date - reference.canonical_period_end).days
        return FiscalIdentityResult(
            fiscal_year=identity[0],
            fiscal_quarter=identity[1],
            canonical_period_end=reference.canonical_period_end,
            alignment_method="EXACT" if alignment_days == 0 else "NEAREST_35D",
            alignment_days=alignment_days,
            alignment_reference_id=reference.id,
        )

    return FiscalIdentityResult(
        fiscal_year=identity[0],
        fiscal_quarter=identity[1],
        canonical_period_end=source_date,
        alignment_method=identity_method or "SEC_CALENDAR",
        alignment_days=0,
        alignment_reference_id=None,
    )


def normalize_yahoo_raw_row(
    row: dict,
    source_variant: str,
    sec_calendar: Sequence[NormalizedObservation],
) -> NormalizedObservation:
    """Normaliza Yahoo conservando sus fechas y sin seleccionar una fuente efectiva."""

    if row.get("source") != "YAHOO":
        raise ValueError("normalize_yahoo_raw_row sólo acepta evidencia YAHOO")
    if not (
        source_variant.startswith("yahoo.")
        or source_variant.startswith("yfinance.")
    ):
        raise ValueError("source_variant Yahoo no admitida")
    observed_at = row.get("fetched_at")
    if not isinstance(observed_at, datetime):
        raise ValueError("Una fila raw Yahoo requiere fetched_at para observed_at")

    payload = row.get("source_payload") or {}
    payload_variant = payload.get("source_variant")
    if payload_variant is not None and payload_variant != source_variant:
        raise ValueError("source_variant Yahoo contradice el payload raw")

    identity = resolve_yahoo_fiscal_identity(row, sec_calendar)
    quality = "OK" if not identity.reasons else "REVIEW_REQUIRED"
    eligibility = "ELIGIBLE" if quality == "OK" else "INELIGIBLE"
    period_end = row["period_end"]
    return NormalizedObservation(
        company_id=row["company_id"],
        metric=row["metric"],
        source="YAHOO",
        source_variant=source_variant,
        observation_kind="REPORTED",
        value=Decimal(str(row["value"])),
        unit=row["unit"],
        currency=row.get("currency"),
        source_period_start=row.get("period_start"),
        source_period_end=period_end,
        canonical_period_end=identity.canonical_period_end,
        series_date=period_end,
        fiscal_year=identity.fiscal_year,
        fiscal_quarter=identity.fiscal_quarter,
        filed_date=None,
        source_available_at=None,
        observed_at=observed_at,
        raw_id=row["id"],
        normalizer_version=YAHOO_NORMALIZER_VERSION,
        intrinsic_quality_status=quality,
        intrinsic_quality_reasons=identity.reasons,
        selection_eligibility=eligibility,
        alignment_method=identity.alignment_method,
        alignment_days=identity.alignment_days,
        alignment_reference_id=identity.alignment_reference_id,
    )


def normalize_yahoo_raw_row_v2(
    row: dict,
    source_variant: str,
    sec_calendar: Sequence[NormalizedObservation],
) -> NormalizedObservation:
    """Normaliza Yahoo v2 sin inventar semántica ausente en evidencia histórica."""

    item = normalize_yahoo_raw_row(row, source_variant, sec_calendar)
    payload = row.get("source_payload") or {}
    source_metric_name = payload.get("source_metric_name")
    if source_variant == "yahoo.fundamentals_timeseries":
        source_metric_name = source_metric_name or str(payload.get("provider_id", "")).split(":", 1)[0]
    metric = item.metric
    reasons = item.intrinsic_quality_reasons
    quality = item.intrinsic_quality_status
    eligibility = item.selection_eligibility
    if source_variant == "yfinance.quarterly_income_stmt" and metric in {
        "EPS_DILUTED", "EPS_BASIC"
    }:
        if source_metric_name in {"Basic EPS", "BasicEPS"}:
            metric = "EPS_BASIC"
        elif source_metric_name in {"Diluted EPS", "DilutedEPS"}:
            metric = "EPS_DILUTED"
        else:
            metric = "EPS_UNSPECIFIED"
            source_metric_name = "legacy.unknown"
            reasons = tuple(dict.fromkeys((*reasons, "LEGACY_YFINANCE_EPS_SEMANTICS_UNPROVEN_V2")))
            quality = "REVIEW_REQUIRED"
            eligibility = "INELIGIBLE"
    if not source_metric_name:
        source_metric_name = str(payload.get("provider_id", row["metric"])).split(":", 1)[0]
    return replace(
        item,
        metric=metric,
        normalizer_version=YAHOO_NORMALIZER_V2,
        source_metric_name=source_metric_name,
        source_unit=payload.get("source_unit") or row["unit"],
        source_scale_factor=Decimal(str(payload.get("source_scale_factor", "1"))),
        intrinsic_quality_reasons=reasons,
        intrinsic_quality_status=quality,
        selection_eligibility=eligibility,
    )


def _observation_values(observation: NormalizedObservation) -> tuple:
    return (
        observation.company_id, observation.metric, observation.source, observation.source_variant,
        observation.observation_kind, observation.value, observation.unit, observation.currency,
        observation.source_period_start, observation.source_period_end, observation.canonical_period_end,
        observation.series_date, observation.fiscal_year, observation.fiscal_quarter, observation.filed_date,
        observation.source_available_at, observation.observed_at, observation.raw_id,
        observation.normalizer_version, observation.intrinsic_quality_status,
        Jsonb(list(observation.intrinsic_quality_reasons)), observation.selection_eligibility,
        observation.alignment_method, observation.alignment_days, observation.alignment_reference_id,
        observation.source_metric_name, observation.source_unit, observation.source_scale_factor,
    )


def _canonical_semantic_value(value):
    if isinstance(value, Jsonb):
        value = value.obj
    if isinstance(value, Decimal):
        return ("decimal", str(value.normalize()))
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return ("datetime", value.isoformat())
    if isinstance(value, date):
        return ("date", value.isoformat())
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                return _canonical_semantic_value(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_canonical_semantic_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _canonical_semantic_value(item)) for key, item in value.items()))
    return value


def _assert_same_semantics(observation: NormalizedObservation, stored: tuple) -> None:
    expected = _observation_values(observation)
    # JSONB is written as Jsonb but read as a list; compare the payload, not the wrapper.
    expected = (*expected[:20], list(observation.intrinsic_quality_reasons), *expected[21:])
    if len(stored) != len(expected) or any(
        _canonical_semantic_value(actual) != _canonical_semantic_value(wanted)
        for actual, wanted in zip(stored, expected)
    ):
        raise RuntimeError(
            "Conflicto semántico para la misma evidencia raw y versión de normalizador."
        )


def insert_normalized_batch(observations: Iterable[NormalizedObservation]) -> list[int]:
    """Inserta observaciones sin sobrescribir versiones previas."""

    observations = list(observations)
    if not observations:
        return []

    ids = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for observation in observations:
                cur.execute(INSERT_NORMALIZED_SQL, _observation_values(observation))
                inserted = cur.fetchone()
                if inserted is not None:
                    ids.append(inserted[0])
                    continue
                cur.execute(
                    FIND_NORMALIZED_SQL,
                    (observation.raw_id, observation.normalizer_version),
                )
                existing = cur.fetchone()
                if existing is None:
                    raise RuntimeError("No se pudo insertar ni localizar el fundamental normalizado.")
                _assert_same_semantics(observation, existing[1:])
                ids.append(existing[0])
    return ids


def load_raw_fundamentals(company_id: int, source: str | None = None) -> list[dict]:
    """Carga evidencia raw ordenada, sin aplicar normalización ni selección."""

    sql = LOAD_RAW_SQL
    params: tuple = (company_id,)
    if source is not None:
        sql += " AND source = %s\n"
        params = (company_id, source)
    sql += " ORDER BY period_end, metric, filed_date, id;"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [description.name for description in cur.description]
            return [dict(zip(columns, values)) for values in cur.fetchall()]
