"""Diagnóstico reproducible de fundamentales de código frente a PostgreSQL.

No persiste datos. La comparación SEC usa ``period_end`` exacto; la capa
híbrida replica la alineación de Yahoo a +/-35 días de ``fundamental_c.py``.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from typing import Iterable, Optional


METRICS = ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")
HYBRID_METRICS = ("EPS_DILUTED", "REVENUE", "NET_INCOME")
EPS_ABS_TOLERANCE = 1e-8
EPS_REL_TOLERANCE = 1e-7
LARGE_VALUE_ABS_TOLERANCE = 0.01
LARGE_VALUE_REL_TOLERANCE = 1e-10
HYBRID_DATE_TOLERANCE_DAYS = 35
YOY_DATE_TOLERANCE_DAYS = 45


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def numeric_status(metric: str, code_value, postgres_value):
    """Devuelve estado, diferencia absoluta y diferencia relativa."""

    code_number = _as_float(code_value)
    postgres_number = _as_float(postgres_value)
    if code_number is None or postgres_number is None:
        return "OTHER", None, None
    absolute = abs(code_number - postgres_number)
    scale = max(abs(code_number), abs(postgres_number))
    relative = absolute / scale if scale else 0.0
    if code_number == postgres_number:
        status = "EXACT_MATCH"
    else:
        abs_tol, rel_tol = (
            (EPS_ABS_TOLERANCE, EPS_REL_TOLERANCE)
            if metric == "EPS_DILUTED"
            else (LARGE_VALUE_ABS_TOLERANCE, LARGE_VALUE_REL_TOLERANCE)
        )
        status = "NUMERIC_TOLERANCE" if absolute <= abs_tol or relative <= rel_tol else "OTHER"
    return status, absolute, relative


def _comparison_row(metric, code=None, postgres=None, status=None):
    code_value = code.get("value") if code else None
    postgres_value = postgres.get("value") if postgres else None
    absolute = relative = None
    if code is not None and postgres is not None:
        numeric, absolute, relative = numeric_status(metric, code_value, postgres_value)
        status = status or numeric
    return {
        "metric": metric,
        "source_code": code.get("source") if code else None,
        "period_code": code.get("period") if code else None,
        "value_code": code_value,
        "tag_code": code.get("tag") if code else None,
        "period_postgres": postgres.get("period") if postgres else None,
        "value_postgres": postgres_value,
        "tag_postgres": postgres.get("tag") if postgres else None,
        "filed_date": postgres.get("filed_date") if postgres else None,
        "source_raw_id": postgres.get("raw_id") if postgres else None,
        "fiscal_year": postgres.get("fiscal_year") if postgres else None,
        "fiscal_quarter": postgres.get("fiscal_quarter") if postgres else None,
        "absolute_diff": absolute,
        "relative_diff": relative,
        "status": status,
    }


def compare_sec_records(metric: str, code_records: Iterable[dict], postgres_records: Iterable[dict]):
    """Compara SEC por ``period_end`` exacto, sin alineación tolerante."""

    code_records = sorted(code_records, key=lambda item: item["period"])
    postgres_by_period = {item["period"]: item for item in postgres_records}
    used = set()
    rows = []
    for code in code_records:
        postgres = postgres_by_period.get(code["period"])
        if postgres is None:
            rows.append(_comparison_row(metric, code=code, status="MISSING_IN_POSTGRES"))
        else:
            used.add(postgres["period"])
            rows.append(_comparison_row(metric, code=code, postgres=postgres))
    for postgres in sorted(postgres_records, key=lambda item: item["period"]):
        if postgres["period"] not in used:
            rows.append(_comparison_row(metric, postgres=postgres, status="MISSING_IN_CODE"))
    return rows


def _nearest_unused(records, target: date, used: set, max_days: int):
    candidates = [
        (abs((item["period"] - target).days), item)
        for item in records
        if item["period"] not in used and abs((item["period"] - target).days) <= max_days
    ]
    return min(candidates, key=lambda pair: (pair[0], pair[1]["period"]))[1] if candidates else None


def compare_hybrid_records(metric: str, code_records: Iterable[dict], postgres_records: Iterable[dict]):
    """Compara híbrido usando exactitud SEC y alineación Yahoo a +/-35 días."""

    code_records = sorted(code_records, key=lambda item: item["period"])
    postgres_records = sorted(postgres_records, key=lambda item: item["period"])
    postgres_by_period = {item["period"]: item for item in postgres_records}
    used = set()
    rows = []
    for code in code_records:
        if code.get("source") == "SEC":
            postgres = postgres_by_period.get(code["period"])
        else:
            postgres = _nearest_unused(
                postgres_records, code["period"], used, HYBRID_DATE_TOLERANCE_DAYS
            )
        if postgres is None:
            missing_status = "YAHOO_FALLBACK" if code.get("source") == "YAHOO" else "MISSING_IN_POSTGRES"
            rows.append(_comparison_row(metric, code=code, status=missing_status))
            continue
        used.add(postgres["period"])
        numeric, _, _ = numeric_status(metric, code["value"], postgres["value"])
        if code["period"] != postgres["period"] and numeric in {"EXACT_MATCH", "NUMERIC_TOLERANCE"}:
            status = "DATE_ALIGNMENT"
        else:
            status = numeric
        rows.append(_comparison_row(metric, code=code, postgres=postgres, status=status))
    for postgres in postgres_records:
        if postgres["period"] not in used:
            rows.append(_comparison_row(metric, postgres=postgres, status="MISSING_IN_CODE"))
    return rows


def _series_records(series, source: str, tag=None):
    return [
        {"period": item_date.date(), "value": float(value), "source": source, "tag": tag}
        for item_date, value in series.dropna().sort_index().items()
    ]


def _hybrid_records(sec_series, yahoo_series, tag=None):
    from fundamental_c import _same_quarter_match

    records = _series_records(sec_series, "SEC", tag)
    for item_date, value in yahoo_series.dropna().sort_index().items():
        if _same_quarter_match(sec_series, item_date, HYBRID_DATE_TOLERANCE_DAYS) is None:
            records.append({"period": item_date.date(), "value": float(value), "source": "YAHOO", "tag": None})
    return sorted(records, key=lambda item: item["period"])


def load_code_series(ticker: str):
    """Ejecuta las mismas extracciones SEC/Yahoo y merges del módulo C."""

    import pandas as pd
    import yfinance as yf
    from fundamental_c import (
        _extract_row,
        _fetch_sec_companyfacts,
        _fetch_yahoo_timeseries,
        _merge_primary_with_fallback,
        _series_to_quarters,
    )

    stock = yf.Ticker(ticker)
    errors = {}
    try:
        income = stock.quarterly_income_stmt
    except Exception as exc:
        income = pd.DataFrame()
        errors["yfinance_income"] = str(exc)

    yf_series = {
        "EPS_DILUTED": _series_to_quarters(
            _extract_row(income, ["Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"])
        ),
        "REVENUE": _series_to_quarters(_extract_row(income, ["Total Revenue", "Operating Revenue"])),
        "NET_INCOME": _series_to_quarters(
            _extract_row(income, ["Net Income", "Net Income Common Stockholders"])
        ),
    }
    yahoo_types = {
        "EPS_DILUTED": "quarterlyDilutedEPS",
        "REVENUE": "quarterlyTotalRevenue",
        "NET_INCOME": "quarterlyNetIncome",
    }
    yahoo_ts, yahoo_error = _fetch_yahoo_timeseries(ticker, list(yahoo_types.values()), years=5)
    if yahoo_error:
        errors["yahoo_timeseries"] = yahoo_error
    yahoo = {
        metric: _merge_primary_with_fallback(yahoo_ts.get(series_type, pd.Series(dtype="float64")), yf_series[metric])
        for metric, series_type in yahoo_types.items()
    }

    sec_data, sec_tags, cik, cik_source, sec_error = _fetch_sec_companyfacts(ticker, stock=stock, years=6)
    if sec_error:
        errors["sec"] = sec_error
    sec = {metric: sec_data.get(metric, pd.Series(dtype="float64")) for metric in METRICS}
    hybrid = {
        metric: _hybrid_records(sec[metric], yahoo[metric], sec_tags.get(metric))
        for metric in HYBRID_METRICS
    }
    return {
        "ticker": ticker,
        "cik": cik,
        "cik_source": cik_source,
        "sec": sec,
        "sec_tags": sec_tags,
        "yahoo": yahoo,
        "hybrid": hybrid,
        "errors": errors,
    }


def load_postgres_records(ticker: str):
    """Lee exclusivamente las filas normalizadas de PostgreSQL."""

    from database.db import get_connection

    query = """
        SELECT
            fq.period_end,
            fq.fiscal_year,
            fq.fiscal_quarter,
            fq.latest_filed_date,
            fq.eps_diluted,
            fq.revenue,
            fq.net_income,
            fq.diluted_shares,
            fq.eps_xbrl_tag,
            fq.revenue_xbrl_tag,
            fq.net_income_xbrl_tag,
            fq.diluted_shares_xbrl_tag,
            fq.eps_raw_id,
            fq.revenue_raw_id,
            fq.net_income_raw_id,
            fq.diluted_shares_raw_id
        FROM fundamentals_quarterly AS fq
        JOIN companies AS c ON c.id = fq.company_id
        WHERE c.ticker = %s
        ORDER BY fq.period_end
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (ticker,))
            columns = [description.name for description in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    if not rows:
        raise RuntimeError(f"No hay fundamentales normalizados para {ticker} en PostgreSQL.")

    fields = {
        "EPS_DILUTED": ("eps_diluted", "eps_xbrl_tag", "eps_raw_id"),
        "REVENUE": ("revenue", "revenue_xbrl_tag", "revenue_raw_id"),
        "NET_INCOME": ("net_income", "net_income_xbrl_tag", "net_income_raw_id"),
        "DILUTED_SHARES": ("diluted_shares", "diluted_shares_xbrl_tag", "diluted_shares_raw_id"),
    }
    records = {metric: [] for metric in METRICS}
    for row in rows:
        for metric, (value_field, tag_field, raw_id_field) in fields.items():
            if row[value_field] is None:
                continue
            records[metric].append(
                {
                    "period": row["period_end"],
                    "value": float(row[value_field]),
                    "source": "SEC",
                    "tag": row[tag_field],
                    "filed_date": row["latest_filed_date"],
                    "raw_id": row[raw_id_field],
                    "fiscal_year": row["fiscal_year"],
                    "fiscal_quarter": row["fiscal_quarter"],
                }
            )
    return records


def _nearest_period(records, target: date, days: int):
    candidates = [(abs((item["period"] - target).days), item) for item in records]
    candidates = [candidate for candidate in candidates if candidate[0] <= days]
    return min(candidates, key=lambda item: (item[0], item[1]["period"]))[1] if candidates else None


def _growth_records(records: list[dict]):
    result = []
    for current in sorted(records, key=lambda item: item["period"]):
        try:
            target = current["period"].replace(year=current["period"].year - 1)
        except ValueError:
            target = current["period"].replace(year=current["period"].year - 1, day=28)
        prior = _nearest_period(records, target, YOY_DATE_TOLERANCE_DAYS)
        if prior is None or prior["value"] <= 0:
            continue
        result.append(
            {
                "period": current["period"],
                "comparable": prior["period"],
                "current_value": current["value"],
                "comparable_value": prior["value"],
                "value": (current["value"] / prior["value"] - 1.0) * 100.0,
                "source": current["source"],
            }
        )
    return result


def _combined_growth(code_data, metric):
    sec_records = _series_records(code_data["sec"][metric], "SEC", code_data["sec_tags"].get(metric))
    yahoo_records = _series_records(code_data["yahoo"][metric], "YAHOO")
    sec_growth = _growth_records(sec_records)
    yahoo_growth = _growth_records(yahoo_records)
    combined = list(sec_growth)
    for growth in yahoo_growth:
        if _nearest_period(sec_records, growth["period"], HYBRID_DATE_TOLERANCE_DAYS) is None:
            combined.append(growth)
    return sorted(combined, key=lambda item: item["period"])


def _period_values_reproducible(growth, postgres_records, metric):
    current = next((item for item in postgres_records if item["period"] == growth["period"]), None)
    prior = next((item for item in postgres_records if item["period"] == growth["comparable"]), None)
    if current is None or prior is None:
        return False
    accepted = {"EXACT_MATCH", "NUMERIC_TOLERANCE"}
    return (
        numeric_status(metric, growth["current_value"], current["value"])[0] in accepted
        and numeric_status(metric, growth["comparable_value"], prior["value"])[0] in accepted
    )


def c_score_inputs(code_data, postgres):
    eps_growth = _combined_growth(code_data, "EPS_DILUTED")
    revenue_growth = _combined_growth(code_data, "REVENUE")
    latest_eps_growth = eps_growth[-1] if eps_growth else None
    previous_eps_growth = eps_growth[-2] if len(eps_growth) >= 2 else None
    latest_revenue_growth = revenue_growth[-1] if revenue_growth else None
    previous_revenue_growth = revenue_growth[-2] if len(revenue_growth) >= 2 else None

    if previous_eps_growth and latest_eps_growth and not 70 <= (
        latest_eps_growth["period"] - previous_eps_growth["period"]
    ).days <= 120:
        previous_eps_growth = None
    if previous_revenue_growth and latest_revenue_growth and not 70 <= (
        latest_revenue_growth["period"] - previous_revenue_growth["period"]
    ).days <= 120:
        previous_revenue_growth = None

    latest_eps_record = max(code_data["hybrid"]["EPS_DILUTED"], key=lambda item: item["period"])
    source_eps_records = _series_records(
        code_data["sec"]["EPS_DILUTED"] if latest_eps_record["source"] == "SEC" else code_data["yahoo"]["EPS_DILUTED"],
        latest_eps_record["source"],
    )
    try:
        target = latest_eps_record["period"].replace(year=latest_eps_record["period"].year - 1)
    except ValueError:
        target = latest_eps_record["period"].replace(year=latest_eps_record["period"].year - 1, day=28)
    previous_eps_value = _nearest_period(source_eps_records, target, YOY_DATE_TOLERANCE_DAYS)
    loss_to_profit = bool(previous_eps_value and previous_eps_value["value"] <= 0 < latest_eps_record["value"])

    def reproducible(growth, metric):
        return bool(
            growth
            and growth["source"] == "SEC"
            and _period_values_reproducible(growth, postgres[metric], metric)
        )

    last_four = eps_growth[-4:]
    last_four_reproducible = len(last_four) >= 3 and all(reproducible(item, "EPS_DILUTED") for item in last_four)
    latest_eps_postgres = next(
        (item for item in postgres["EPS_DILUTED"] if item["period"] == latest_eps_record["period"]),
        None,
    )
    latest_eps_value_reproducible = bool(
        latest_eps_postgres
        and numeric_status(
            "EPS_DILUTED", latest_eps_record["value"], latest_eps_postgres["value"]
        )[0] in {"EXACT_MATCH", "NUMERIC_TOLERANCE"}
    )
    latest_eps_yoy = latest_eps_growth["value"] if latest_eps_growth else None
    previous_eps_yoy = previous_eps_growth["value"] if previous_eps_growth else None
    inferred_previous_eps = (
        latest_eps_record["value"] / (1.0 + latest_eps_yoy / 100.0)
        if latest_eps_yoy is not None and 1.0 + latest_eps_yoy / 100.0 > 0
        else None
    )

    return [
        _input("latest_eps_yoy_pct", latest_eps_growth, reproducible(latest_eps_growth, "EPS_DILUTED")),
        _input("previous_eps_yoy_pct", previous_eps_growth, reproducible(previous_eps_growth, "EPS_DILUTED")),
        _input_acceleration("eps_acceleration_pp", latest_eps_growth, previous_eps_growth, "EPS_DILUTED", postgres),
        {
            "input": "last_4_eps_yoy_pct",
            "value": [item["value"] for item in last_four],
            "period": ", ".join(str(item["period"]) for item in last_four),
            "comparable": ", ".join(str(item["comparable"]) for item in last_four),
            "source": ", ".join(item["source"] for item in last_four),
            "reproducible": "SÍ" if last_four_reproducible else "PARCIAL",
            "reason": "Todos los pares están en PostgreSQL." if last_four_reproducible else "Falta al menos un par YoY o procede de Yahoo.",
        },
        _input("latest_revenue_yoy_pct", latest_revenue_growth, reproducible(latest_revenue_growth, "REVENUE")),
        _input("previous_revenue_yoy_pct", previous_revenue_growth, reproducible(previous_revenue_growth, "REVENUE")),
        _input_acceleration("revenue_acceleration_pp", latest_revenue_growth, previous_revenue_growth, "REVENUE", postgres),
        {
            "input": "eps_loss_to_profit",
            "value": loss_to_profit,
            "period": latest_eps_record["period"],
            "comparable": previous_eps_value["period"] if previous_eps_value else None,
            "source": latest_eps_record["source"],
            "reproducible": "SÍ" if latest_eps_value_reproducible and previous_eps_value and latest_eps_record["source"] == "SEC" else "NO",
            "reason": "Último EPS y comparable disponibles." if latest_eps_value_reproducible and previous_eps_value else "Falta último EPS o comparable.",
        },
        {
            "input": "BASE_EFFECT_RISK inputs",
            "value": {"previous_eps_yoy_pct": previous_eps_yoy, "latest_eps_yoy_pct": latest_eps_yoy},
            "period": latest_eps_growth["period"] if latest_eps_growth else None,
            "comparable": previous_eps_growth["period"] if previous_eps_growth else None,
            "source": latest_eps_growth["source"] if latest_eps_growth else None,
            "reproducible": "SÍ" if reproducible(latest_eps_growth, "EPS_DILUTED") and reproducible(previous_eps_growth, "EPS_DILUTED") else "PARCIAL",
            "reason": "Ambos crecimientos reproducibles." if reproducible(latest_eps_growth, "EPS_DILUTED") and reproducible(previous_eps_growth, "EPS_DILUTED") else "Falta algún crecimiento.",
        },
        {
            "input": "SMALL_BASE_RISK inputs",
            "value": {"latest_eps": latest_eps_record["value"], "latest_eps_yoy_pct": latest_eps_yoy, "inferred_previous_eps": inferred_previous_eps},
            "period": latest_eps_record["period"],
            "comparable": latest_eps_growth["comparable"] if latest_eps_growth else None,
            "source": latest_eps_record["source"],
            "reproducible": "SÍ" if latest_eps_value_reproducible and reproducible(latest_eps_growth, "EPS_DILUTED") else "PARCIAL",
            "reason": "EPS actual y crecimiento reproducibles." if latest_eps_value_reproducible and reproducible(latest_eps_growth, "EPS_DILUTED") else "Falta EPS actual o crecimiento.",
        },
    ]


def _input(name, growth, reproducible):
    return {
        "input": name,
        "value": growth["value"] if growth else None,
        "period": growth["period"] if growth else None,
        "comparable": growth["comparable"] if growth else None,
        "source": growth["source"] if growth else None,
        "reproducible": "SÍ" if reproducible else "NO",
        "reason": "Par YoY presente en PostgreSQL." if reproducible else "Falta un período o la fuente es Yahoo.",
    }


def _input_acceleration(name, latest, previous, metric, postgres):
    latest_ok = bool(
        latest
        and latest["source"] == "SEC"
        and _period_values_reproducible(latest, postgres[metric], metric)
    )
    previous_ok = bool(
        previous
        and previous["source"] == "SEC"
        and _period_values_reproducible(previous, postgres[metric], metric)
    )
    return {
        "input": name,
        "value": latest["value"] - previous["value"] if latest and previous else None,
        "period": latest["period"] if latest else None,
        "comparable": previous["period"] if previous else None,
        "source": latest["source"] if latest else None,
        "reproducible": "SÍ" if latest_ok and previous_ok else "NO",
        "reason": "Ambos crecimientos consecutivos reproducibles." if latest_ok and previous_ok else "Falta alguno de los dos crecimientos.",
    }


def comparison_summary(rows):
    matched = [row for row in rows if row["period_code"] is not None and row["period_postgres"] is not None]
    return {
        "code": sum(row["period_code"] is not None for row in rows),
        "postgres": sum(row["period_postgres"] is not None for row in rows),
        "common": len(matched),
        "exact": sum(row["status"] == "EXACT_MATCH" for row in rows),
        "tolerance": sum(row["status"] == "NUMERIC_TOLERANCE" for row in rows),
        "missing_postgres": sum(row["status"] in {"MISSING_IN_POSTGRES", "YAHOO_FALLBACK"} for row in rows),
        "missing_code": sum(row["status"] == "MISSING_IN_CODE" for row in rows),
        "different": sum(row["status"] == "OTHER" for row in rows),
    }


def reproduced_count(rows):
    """Cuenta sólo coincidencias numéricas demostradas."""

    return sum(
        row["status"] in {"EXACT_MATCH", "NUMERIC_TOLERANCE", "DATE_ALIGNMENT"}
        for row in rows
    )


def q4_coverage(code_data, postgres):
    rows = []
    for fiscal_year in range(2020, 2026):
        for metric in METRICS:
            sec_records = _series_records(code_data["sec"][metric], "SEC")
            sec_growth_periods = {item["period"] for item in _growth_records(sec_records)}
            pg_q4 = next(
                (
                    item
                    for item in postgres[metric]
                    if item.get("fiscal_year") == fiscal_year and item.get("fiscal_quarter") == 4
                ),
                None,
            )
            sec_q4 = next(
                (
                    item
                    for item in sec_records
                    if item["period"].year == fiscal_year and item["period"].month == 9
                ),
                None,
            )
            if metric in HYBRID_METRICS:
                yahoo_records = _series_records(code_data["yahoo"][metric], "YAHOO")
                hybrid_records = code_data["hybrid"][metric]
                yahoo_growth_periods = {item["period"] for item in _growth_records(yahoo_records)}
                yahoo_q4 = next(
                    (
                        item
                        for item in yahoo_records
                        if item["period"].year == fiscal_year and item["period"].month == 9
                    ),
                    None,
                )
                hybrid_q4 = next(
                    (
                        item
                        for item in hybrid_records
                        if item["period"].year == fiscal_year and item["period"].month == 9
                    ),
                    None,
                )
                yoy = bool(yahoo_q4 and yahoo_q4["period"] in yahoo_growth_periods) or bool(
                    sec_q4 and sec_q4["period"] in sec_growth_periods
                )
                yahoo_available = bool(yahoo_q4)
                hybrid_available = bool(hybrid_q4)
                yahoo_date = yahoo_q4["period"] if yahoo_q4 else None
            else:
                yahoo_available = hybrid_available = "N/A"
                yahoo_date = None
                yoy = "N/A"
            if sec_q4:
                reason = "SEC explícito."
            elif yahoo_available is True:
                reason = "Fallback Yahoo disponible."
            elif metric == "DILUTED_SHARES":
                reason = "Sin hecho SEC trimestral explícito; Yahoo no participa en esta serie."
            else:
                reason = "Sin hecho SEC trimestral explícito ni Yahoo disponible en la ventana actual."
            rows.append(
                {
                    "fiscal_year": fiscal_year,
                    "metric": metric,
                    "sec": bool(sec_q4),
                    "postgres": bool(pg_q4),
                    "yahoo": yahoo_available,
                    "hybrid": hybrid_available,
                    "yahoo_date": yahoo_date,
                    "yoy": yoy,
                    "reason": reason,
                }
            )
    return rows


def _fmt_value(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value)


def _print_rows(title, rows):
    print(f"\n{title}")
    print(
        "metric | src | code_date | code_value | code_tag | pg_date | pg_value | "
        "pg_tag | filed | raw_id | FY/Q | abs_diff | rel_diff | status"
    )
    for row in rows:
        fiscal = (
            f"{row['fiscal_year']}/Q{row['fiscal_quarter']}"
            if row["fiscal_year"] is not None and row["fiscal_quarter"] is not None
            else "-"
        )
        print(" | ".join(
            _fmt_value(value) for value in (
                row["metric"], row["source_code"], row["period_code"], row["value_code"], row["tag_code"],
                row["period_postgres"], row["value_postgres"], row["tag_postgres"], row["filed_date"],
                row["source_raw_id"], fiscal, row["absolute_diff"], row["relative_diff"], row["status"],
            )
        ))


def run_validation(ticker: str):
    ticker = ticker.upper().strip()
    code_data = load_code_series(ticker)
    postgres = load_postgres_records(ticker)
    sec_rows = {}
    hybrid_rows = {}
    for metric in METRICS:
        code_records = _series_records(code_data["sec"][metric], "SEC", code_data["sec_tags"].get(metric))
        sec_rows[metric] = compare_sec_records(metric, code_records, postgres[metric])
    for metric in HYBRID_METRICS:
        hybrid_rows[metric] = compare_hybrid_records(metric, code_data["hybrid"][metric], postgres[metric])

    print(f"VALIDACIÓN FUNDAMENTALS {ticker}")
    print(f"CIK: {code_data['cik']} ({code_data['cik_source']})")
    print(
        "Tolerancias: EPS abs<=1e-8 o rel<=1e-7; valores grandes "
        "abs<=0.01 o rel<=1e-10; Yahoo fecha<=35 días."
    )
    if code_data["errors"]:
        print(f"Errores/fallbacks de fuentes: {code_data['errors']}")

    for metric in METRICS:
        _print_rows(f"SEC VS POSTGRESQL — {metric} — tag código: {code_data['sec_tags'].get(metric)}", sec_rows[metric])
        print(f"Resumen: {comparison_summary(sec_rows[metric])}")
    for metric in HYBRID_METRICS:
        _print_rows(f"HÍBRIDO VS POSTGRESQL — {metric}", hybrid_rows[metric])
        print(f"Resumen: {comparison_summary(hybrid_rows[metric])}")

    q4_rows = q4_coverage(code_data, postgres)
    print("\nQ4 COVERAGE (AAPL: septiembre se identifica sólo para diagnóstico; no se deriva ningún valor)")
    print("FY | metric | SEC | PostgreSQL | Yahoo | Híbrido | Yahoo date | YoY | motivo")
    for row in q4_rows:
        print(" | ".join(_fmt_value(row[key]) for key in (
            "fiscal_year", "metric", "sec", "postgres", "yahoo", "hybrid", "yahoo_date", "yoy", "reason"
        )))

    inputs = c_score_inputs(code_data, postgres)
    print("\nC-SCORE INPUT REPRODUCIBILITY")
    print("input | value | current | comparable/previous | source | reproducible | reason")
    for item in inputs:
        print(" | ".join(_fmt_value(item[key]) for key in (
            "input", "value", "period", "comparable", "source", "reproducible", "reason"
        )))

    print("\nQUANTITATIVE SUMMARY")
    sec_numerator = sec_denominator = 0
    for metric, rows in sec_rows.items():
        summary = comparison_summary(rows)
        reproduced = reproduced_count(rows)
        denominator = summary["code"]
        sec_numerator += reproduced
        sec_denominator += denominator
        print(f"SEC {metric}: {reproduced}/{denominator} ({100 * reproduced / denominator:.1f}%)")
    hybrid_numerator = hybrid_denominator = 0
    for metric, rows in hybrid_rows.items():
        summary = comparison_summary(rows)
        reproduced = reproduced_count(rows)
        denominator = summary["code"]
        hybrid_numerator += reproduced
        hybrid_denominator += denominator
        print(f"HYBRID {metric}: {reproduced}/{denominator} ({100 * reproduced / denominator:.1f}%)")
    input_exact = sum(item["reproducible"] == "SÍ" for item in inputs)
    print(f"SEC GLOBAL: {sec_numerator}/{sec_denominator} ({100 * sec_numerator / sec_denominator:.1f}%)")
    print(f"HYBRID GLOBAL: {hybrid_numerator}/{hybrid_denominator} ({100 * hybrid_numerator / hybrid_denominator:.1f}%)")
    print(f"C INPUTS: {input_exact}/{len(inputs)} grupos reproducibles desde PostgreSQL")

    return {"sec": sec_rows, "hybrid": hybrid_rows, "q4": q4_rows, "inputs": inputs}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Ticker a validar, por ejemplo AAPL")
    args = parser.parse_args(argv)
    run_validation(args.ticker)


if __name__ == "__main__":
    main()
