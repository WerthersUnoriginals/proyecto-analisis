"""Persistencia append-only de observaciones fundamentales normalizadas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable

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

_METRIC_UNITS = {
    "EPS_DILUTED": "USD/shares",
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


_NORMALIZED_COLUMNS = (
    "company_id", "metric", "source", "source_variant", "observation_kind", "value", "unit",
    "currency", "source_period_start", "source_period_end", "canonical_period_end", "series_date",
    "fiscal_year", "fiscal_quarter", "filed_date", "source_available_at", "observed_at", "raw_id",
    "normalizer_version", "intrinsic_quality_status", "intrinsic_quality_reasons",
    "selection_eligibility", "alignment_method", "alignment_days", "alignment_reference_id",
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
