from __future__ import annotations

from datetime import date

import pandas as pd
import requests

from database.fundamentals import insert_raw_fundamentals_batch
from fundamental_c import (
    SEC_COMPANYFACTS_URL,
    SEC_HEADERS,
    SEC_TAG_CANDIDATES,
    SEC_UNIT_PREFERENCE,
    _clean_number,
    _discover_diluted_shares_tags,
    _quarter_like_fact,
    _resolve_cik,
)


def _parse_date(value):
    if not value:
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    return ts.date()


def _parse_fiscal_year(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_fiscal_quarter(fp):
    if not fp:
        return None
    text = str(fp).strip().upper()
    if text in {"Q1", "Q2", "Q3", "Q4"}:
        return int(text[1])
    return None


def _currency_from_unit(unit):
    if not unit:
        return None
    return "USD" if str(unit).upper().startswith("USD") else None


def _rows_from_concept(concept: dict, metric: str, xbrl_tag: str, years: int):
    cutoff = (pd.Timestamp.now("UTC").tz_localize(None) - pd.DateOffset(years=years)).date()
    units = concept.get("units", {})
    unit = next((candidate for candidate in SEC_UNIT_PREFERENCE[metric] if candidate in units), None)
    if unit is None:
        return []

    rows = []
    for item in units[unit]:
        if not _quarter_like_fact(item):
            continue

        period_end = _parse_date(item.get("end"))
        value = _clean_number(item.get("val"))
        if period_end is None or value is None or period_end < cutoff:
            continue

        rows.append(
            {
                "period_start": _parse_date(item.get("start")),
                "period_end": period_end,
                "filed_date": _parse_date(item.get("filed")),
                "fiscal_year": _parse_fiscal_year(item.get("fy")),
                "fiscal_quarter": _parse_fiscal_quarter(item.get("fp")),
                "form_type": item.get("form"),
                "value": value,
                "unit": unit,
                "currency": _currency_from_unit(unit),
                "xbrl_tag": xbrl_tag,
                "source_record_id": item.get("accn"),
                "source_payload": item,
            }
        )

    rows.sort(key=lambda row: (row["period_end"], row["filed_date"] or date.min))
    latest_by_period = {}
    for row in rows:
        latest_by_period[row["period_end"]] = row
    return list(latest_by_period.values())


def _candidate_tags(facts: dict, metric: str):
    candidates = list(SEC_TAG_CANDIDATES[metric])
    if metric == "DILUTED_SHARES":
        for tag in _discover_diluted_shares_tags(facts):
            if tag not in candidates:
                candidates.append(tag)
    return candidates


def extract_sec_raw_facts(symbol: str, company_id: int, years: int = 6):
    """Descarga SEC Company Facts y conserva metadatos auditables por trimestre."""

    ticker = symbol.upper().strip()
    cik, cik_source = _resolve_cik(ticker)
    if not cik:
        raise RuntimeError(f"No se pudo resolver el CIK para {ticker}.")

    response = requests.get(
        SEC_COMPANYFACTS_URL.format(cik=cik),
        headers=SEC_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    selected_tags = {}
    facts_to_store = []

    for metric in SEC_TAG_CANDIDATES:
        best_rows = []
        best_tag = None

        for tag in _candidate_tags(payload, metric):
            concept = payload.get("facts", {}).get("us-gaap", {}).get(tag)
            if not concept:
                continue

            rows = _rows_from_concept(concept, metric, tag, years)
            if len(rows) > len(best_rows):
                best_rows = rows
                best_tag = tag

        if best_tag is None:
            continue

        selected_tags[metric] = best_tag
        for row in best_rows:
            row.update(
                {
                    "company_id": company_id,
                    "source": "SEC",
                    "metric": metric,
                }
            )
            facts_to_store.append(row)

    return {
        "ticker": ticker,
        "cik": cik,
        "cik_source": cik_source,
        "selected_tags": selected_tags,
        "facts": facts_to_store,
    }


def import_sec_fundamentals(symbol: str, company_id: int, years: int = 6):
    """Descarga hechos SEC y los persiste en fundamentals_raw."""

    result = extract_sec_raw_facts(symbol, company_id, years=years)
    ids = insert_raw_fundamentals_batch(result["facts"])
    return {
        "ticker": result["ticker"],
        "cik": result["cik"],
        "cik_source": result["cik_source"],
        "selected_tags": result["selected_tags"],
        "facts_found": len(result["facts"]),
        "rows_resolved": len(ids),
        "raw_ids": ids,
    }
