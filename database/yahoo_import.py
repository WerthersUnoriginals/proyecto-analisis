"""Persistencia raw auditable de fundamentales Yahoo por variante."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from database.fundamentals import insert_raw_fundamentals_batch


TIMESERIES_VARIANT = "yahoo.fundamentals_timeseries"
YFINANCE_VARIANT = "yfinance.quarterly_income_stmt"

YAHOO_TYPES = {
    "quarterlyDilutedEPS": ("EPS_DILUTED", "USD/shares"),
    "quarterlyTotalRevenue": ("REVENUE", "USD"),
    "quarterlyNetIncome": ("NET_INCOME", "USD"),
}

YFINANCE_ALIASES = {
    "EPS_DILUTED": ["Diluted EPS", "DilutedEPS"],
    "EPS_BASIC": ["Basic EPS", "BasicEPS"],
    "REVENUE": ["Total Revenue", "Operating Revenue"],
    "NET_INCOME": ["Net Income", "Net Income Common Stockholders"],
}

METRIC_UNITS = {
    "EPS_DILUTED": "USD/shares",
    "EPS_BASIC": "USD/shares",
    "REVENUE": "USD",
    "NET_INCOME": "USD",
}


def canonical_source_record_id(
    source_variant: str, provider_id: str, semantic_payload: dict,
) -> str:
    """Identifica contenido semántico sin incluir el instante de observación."""

    material = {
        "provider_id": provider_id,
        "semantic_payload": semantic_payload,
        "source_variant": source_variant,
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return f"yahoo:{hashlib.sha256(encoded).hexdigest()}"


def _raw_fact(
    company_id: int,
    source_variant: str,
    provider_id: str,
    metric: str,
    period_end: date,
    value,
    unit: str,
    observed_at: datetime,
    source_metric_name: str,
) -> dict:
    decimal_value = Decimal(str(value))
    semantic_payload = {
        "currency": "USD",
        "metric": metric,
        "period": period_end.isoformat(),
        "source_metric_name": source_metric_name,
        "source_scale_factor": "1",
        "source_unit": unit,
        "unit": unit,
        "value": str(decimal_value),
    }
    source_record_id = canonical_source_record_id(
        source_variant, provider_id, semantic_payload,
    )
    return {
        "company_id": company_id,
        "source": "YAHOO",
        "source_variant": source_variant,
        "metric": metric,
        "period_start": None,
        "period_end": period_end,
        "filed_date": None,
        "fiscal_year": None,
        "fiscal_quarter": None,
        "form_type": None,
        "value": decimal_value,
        "unit": unit,
        "currency": "USD",
        "xbrl_tag": None,
        "source_record_id": source_record_id,
        "source_available_at": None,
        "source_metric_name": source_metric_name,
        "source_unit": unit,
        "source_scale_factor": Decimal("1"),
        "fetched_at": observed_at,
        "source_payload": {
            "provider": "Yahoo Finance",
            "provider_id": provider_id,
            "semantic_payload": semantic_payload,
            "source_available_at": None,
            "source_variant": source_variant,
            "source_metric_name": source_metric_name,
            "source_unit": unit,
            "source_scale_factor": "1",
        },
    }


def _extract_named_row(frame, aliases):
    if frame is None or frame.empty:
        return None, None
    normalized = {str(index).strip().lower(): index for index in frame.index}
    for alias in aliases:
        original = normalized.get(alias.strip().lower())
        if original is not None:
            return str(original), frame.loc[original]
    return None, None


def extract_yahoo_raw_facts(
    ticker: str,
    company_id: int,
    observed_at: datetime,
    clients: dict | None = None,
) -> list[dict]:
    """Extrae las dos variantes Yahoo sin mezclarlas ni asignar identidad fiscal."""

    if observed_at.tzinfo is None:
        raise ValueError("observed_at debe incluir zona horaria")

    if clients is None:
        import yfinance as yf
        from fundamental_c import _fetch_yahoo_timeseries

        clients = {
            "timeseries_fetcher": _fetch_yahoo_timeseries,
            "stock": yf.Ticker(ticker),
        }

    timeseries, _ = clients["timeseries_fetcher"](
        ticker, list(YAHOO_TYPES), years=5,
    )
    facts = []
    for series_type, (metric, unit) in YAHOO_TYPES.items():
        series = timeseries.get(series_type)
        if series is None:
            continue
        for item_date, value in series.dropna().sort_index().items():
            period_end = item_date.date()
            facts.append(_raw_fact(
                company_id,
                TIMESERIES_VARIANT,
                f"{series_type}:{period_end.isoformat()}",
                metric,
                period_end,
                value,
                unit,
                observed_at,
                series_type,
            ))

    income = clients["stock"].quarterly_income_stmt
    for metric, aliases in YFINANCE_ALIASES.items():
        source_metric_name, series = _extract_named_row(income, aliases)
        if series is None:
            continue
        for item_date, value in series.dropna().sort_index().items():
            period_end = item_date.date()
            facts.append(_raw_fact(
                company_id,
                YFINANCE_VARIANT,
                f"{source_metric_name}:{period_end.isoformat()}",
                metric,
                period_end,
                value,
                METRIC_UNITS[metric],
                observed_at,
                source_metric_name,
            ))

    return facts


def import_yahoo_fundamentals(
    ticker: str,
    company_id: int,
    observed_at: datetime,
    clients: dict | None = None,
) -> dict:
    """Persiste en un lote todas las evidencias Yahoo encontradas."""

    facts = extract_yahoo_raw_facts(ticker, company_id, observed_at, clients=clients)
    raw_ids = insert_raw_fundamentals_batch(facts)
    return {
        "ticker": ticker.upper().strip(),
        "facts_found": len(facts),
        "rows_resolved": len(raw_ids),
        "by_variant": dict(Counter(fact["source_variant"] for fact in facts)),
        "raw_ids": raw_ids,
    }
