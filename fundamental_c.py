"""
CAN SLIM+ — Módulo C: Current Earnings v2.4

Objetivo:
- Medir crecimiento trimestral de EPS y ventas para la C de CAN SLIM+.
- Usar SEC Company Facts como histórico regulatorio principal en acciones USA.
- Usar Yahoo/yfinance para completar trimestres recientes que SEC no expone
  como hechos trimestrales independientes (especialmente algunos Q4).
- Calcular YoY buscando el trimestre comparable de hace ~1 año, NO por posición.
- Mantener la misma fuente para numerador y denominador del YoY siempre que sea
  posible, evitando mezclar definiciones contables cuando SEC y Yahoo discrepan.

Esta versión NO asigna todavía un score CAN SLIM definitivo.

Requiere:
    pip install yfinance pandas requests
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
import yfinance as yf


# ---------------------------------------------------------------------------
# Configuración SEC
# ---------------------------------------------------------------------------

# CIK conocidos para la batería de validación v2.4. Más adelante se añadirá un
# resolver ticker→CIK general con caché para el universo completo del screener.
CIK_MAP = {
    "NVDA": "0001045810",
    "MSFT": "0000789019",
    "COST": "0000909832",
    "XOM": "0000034088",
    "AMZN": "0001018724",
}

SEC_HEADERS = {
    "User-Agent": "CANSLIMResearch/0.2 educational-research",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

SEC_TAG_CANDIDATES = {
    "EPS_DILUTED": ["EarningsPerShareDiluted"],
    "REVENUE": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "NET_INCOME": ["NetIncomeLoss", "ProfitLoss"],
}

SEC_UNIT_PREFERENCE = {
    "EPS_DILUTED": ["USD/shares", "USD / shares"],
    "REVENUE": ["USD"],
    "NET_INCOME": ["USD"],
}


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------

def _clean_number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _to_timestamp(value) -> Optional[pd.Timestamp]:
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts
    except Exception:
        return None


def _extract_row(df: Optional[pd.DataFrame], possible_names: list[str]) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    normalized = {str(index).strip().lower(): index for index in df.index}
    for name in possible_names:
        key = name.strip().lower()
        if key in normalized:
            return df.loc[normalized[key]]
    return None


def _series_to_quarters(row: Optional[pd.Series]) -> pd.Series:
    if row is None:
        return pd.Series(dtype="float64")
    values = []
    for date, value in row.items():
        number = _clean_number(value)
        ts = _to_timestamp(date)
        if number is not None and ts is not None:
            values.append((ts, number))
    if not values:
        return pd.Series(dtype="float64")
    return pd.Series(dict(sorted(values)), dtype="float64")


def _last_values(series: pd.Series, n: int = 12) -> list[dict]:
    output = []
    for date, value in series.dropna().tail(n).items():
        output.append({"date": pd.Timestamp(date).strftime("%Y-%m-%d"), "value": float(value)})
    return output


def _latest_valid(series: pd.Series) -> Optional[float]:
    valid = series.dropna()
    return None if valid.empty else float(valid.iloc[-1])


def _nearest_index(series: pd.Series, target: pd.Timestamp, max_days: int) -> Optional[pd.Timestamp]:
    if series.empty:
        return None
    candidates = []
    for date in series.index:
        delta = abs((pd.Timestamp(date) - target).days)
        if delta <= max_days:
            candidates.append((delta, pd.Timestamp(date)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _same_quarter_match(series: pd.Series, date: pd.Timestamp, max_days: int = 35) -> Optional[pd.Timestamp]:
    return _nearest_index(series, pd.Timestamp(date), max_days=max_days)


def _comparable_previous_date(series: pd.Series, current_date: pd.Timestamp, max_days: int = 45) -> Optional[pd.Timestamp]:
    target = pd.Timestamp(current_date) - pd.DateOffset(years=1)
    return _nearest_index(series, target, max_days=max_days)


def _growth_yoy_by_date(series: pd.Series) -> pd.Series:
    """YoY por trimestre comparable real, sin depender de i-4 ni de una serie sin huecos."""
    if series.empty:
        return pd.Series(dtype="float64")

    result = pd.Series(index=series.index, dtype="float64")
    for date, value in series.items():
        prior_date = _comparable_previous_date(series, pd.Timestamp(date))
        if prior_date is None:
            result.loc[date] = float("nan")
            continue
        previous = _clean_number(series.loc[prior_date])
        current = _clean_number(value)
        if previous is None or current is None or previous <= 0:
            result.loc[date] = float("nan")
            continue
        result.loc[date] = (current / previous - 1.0) * 100.0
    return result.sort_index()


def _merge_primary_with_fallback(primary: pd.Series, fallback: pd.Series, max_days: int = 35) -> pd.Series:
    """Une observaciones; SEC (primary) manda y Yahoo sólo rellena trimestres ausentes."""
    rows = [(pd.Timestamp(d), float(v)) for d, v in primary.dropna().items()]
    for date, value in fallback.dropna().items():
        date = pd.Timestamp(date)
        if _same_quarter_match(primary, date, max_days=max_days) is None:
            rows.append((date, float(value)))
    if not rows:
        return pd.Series(dtype="float64")
    rows.sort(key=lambda item: item[0])
    return pd.Series(dict(rows), dtype="float64").sort_index()


def _merge_growth_primary_with_fallback(
    primary_raw: pd.Series,
    primary_growth: pd.Series,
    fallback_raw: pd.Series,
    fallback_growth: pd.Series,
    max_days: int = 35,
) -> pd.Series:
    """
    Combina crecimientos ya calculados dentro de cada fuente.
    SEC manda si existe el trimestre; Yahoo entra sólo si SEC no tiene ese trimestre.
    Así un Yahoo actual se compara contra Yahoo del año anterior y no contra SEC.
    """
    rows = []
    for date, value in primary_growth.dropna().items():
        rows.append((pd.Timestamp(date), float(value)))
    for date, value in fallback_growth.dropna().items():
        date = pd.Timestamp(date)
        if _same_quarter_match(primary_raw, date, max_days=max_days) is None:
            rows.append((date, float(value)))
    if not rows:
        return pd.Series(dtype="float64")
    rows.sort(key=lambda item: item[0])
    return pd.Series(dict(rows), dtype="float64").sort_index()


def _consecutive_previous(growth: pd.Series) -> Optional[float]:
    valid = growth.dropna().sort_index()
    if len(valid) < 2:
        return None
    latest_date = pd.Timestamp(valid.index[-1])
    previous_date = pd.Timestamp(valid.index[-2])
    if not 70 <= (latest_date - previous_date).days <= 120:
        return None
    return float(valid.iloc[-2])


def _growth_acceleration(growth: pd.Series) -> Optional[float]:
    latest = _latest_valid(growth)
    previous = _consecutive_previous(growth)
    if latest is None or previous is None:
        return None
    return latest - previous


def _latest_comparable_value(sec: pd.Series, yahoo: pd.Series) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Devuelve (actual, comparable año anterior, fuente) respetando la fuente del actual."""
    hybrid = _merge_primary_with_fallback(sec, yahoo)
    if hybrid.empty:
        return None, None, None
    latest_date = pd.Timestamp(hybrid.index[-1])
    latest_value = float(hybrid.iloc[-1])

    if latest_date in sec.index:
        source = "SEC"
        prior_date = _comparable_previous_date(sec, latest_date)
        prior = float(sec.loc[prior_date]) if prior_date is not None else None
    else:
        source = "YAHOO"
        prior_date = _comparable_previous_date(yahoo, latest_date)
        prior = float(yahoo.loc[prior_date]) if prior_date is not None else None
    return latest_value, prior, source


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------

def _fetch_yahoo_timeseries(
    ticker: str,
    series_types: list[str],
    years: int = 5,
) -> tuple[dict[str, pd.Series], Optional[str]]:
    now = int(time.time())
    period1 = now - years * 366 * 24 * 60 * 60
    period2 = now + 24 * 60 * 60
    result: dict[str, pd.Series] = {}
    errors: list[str] = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }

    for series_type in series_types:
        url = (
            "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/"
            f"finance/timeseries/{ticker}"
        )
        params = {
            "symbol": ticker,
            "type": series_type,
            "period1": period1,
            "period2": period2,
            "padTimeSeries": "true",
            "lang": "en-US",
            "region": "US",
        }
        try:
            response = requests.get(url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{series_type}: {exc}")
            continue

        rows = []
        for item in payload.get("timeseries", {}).get("result", []):
            for value in item.get(series_type, []):
                ts = _to_timestamp(value.get("asOfDate"))
                number = _clean_number(value.get("reportedValue", {}).get("raw"))
                if ts is not None and number is not None:
                    rows.append((ts, number))
        if rows:
            result[series_type] = pd.Series(dict(sorted(rows)), dtype="float64")
        else:
            errors.append(f"{series_type}: sin valores utilizables")

    return result, "; ".join(errors) if errors else None


# ---------------------------------------------------------------------------
# SEC Company Facts
# ---------------------------------------------------------------------------

def _days_between(start: str, end: str) -> Optional[int]:
    s = _to_timestamp(start)
    e = _to_timestamp(end)
    if s is None or e is None:
        return None
    return int((e - s).days)


def _quarter_like_fact(item: dict) -> bool:
    if item.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
        return False
    duration = _days_between(item.get("start"), item.get("end"))
    return duration is not None and 70 <= duration <= 110


def _fetch_sec_companyfacts(symbol: str, years: int = 6) -> tuple[dict[str, pd.Series], dict[str, str], Optional[str]]:
    cik = CIK_MAP.get(symbol)
    if not cik:
        return {}, {}, "CIK no configurado todavía para este ticker en v2.4"

    try:
        response = requests.get(
            SEC_COMPANYFACTS_URL.format(cik=cik),
            headers=SEC_HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        facts = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {}, {}, str(exc)

    cutoff = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None) - pd.DateOffset(years=years)
    series_out: dict[str, pd.Series] = {}
    chosen_tags: dict[str, str] = {}

    for metric, candidates in SEC_TAG_CANDIDATES.items():
        best_series = pd.Series(dtype="float64")
        best_tag = None
        for tag in candidates:
            concept = facts.get("facts", {}).get("us-gaap", {}).get(tag)
            if not concept:
                continue
            units = concept.get("units", {})
            selected_unit = next((u for u in SEC_UNIT_PREFERENCE[metric] if u in units), None)
            if selected_unit is None:
                continue

            rows = []
            for item in units.get(selected_unit, []):
                if not _quarter_like_fact(item):
                    continue
                end = _to_timestamp(item.get("end"))
                value = _clean_number(item.get("val"))
                filed = _to_timestamp(item.get("filed"))
                if end is None or value is None or end < cutoff:
                    continue
                rows.append((end, filed or pd.Timestamp.min, value))

            if not rows:
                continue

            frame = pd.DataFrame(rows, columns=["end", "filed", "value"])
            frame = frame.sort_values(["end", "filed"]).drop_duplicates("end", keep="last")
            candidate_series = pd.Series(
                frame.set_index("end")["value"].astype(float).to_dict(), dtype="float64"
            ).sort_index()
            if len(candidate_series) > len(best_series):
                best_series = candidate_series
                best_tag = tag

        if not best_series.empty:
            series_out[metric] = best_series
            chosen_tags[metric] = best_tag or "N/D"

    return series_out, chosen_tags, None


# ---------------------------------------------------------------------------
# Yahoo auxiliares: sorpresas / estimaciones
# ---------------------------------------------------------------------------

def _history_records(history: pd.DataFrame, n: int = 8) -> list[dict]:
    if history is None or history.empty:
        return []
    frame = history.tail(n).copy().reset_index()
    records = []
    for _, row in frame.iterrows():
        record = {}
        for column, value in row.items():
            if isinstance(value, pd.Timestamp):
                record[column] = value.strftime("%Y-%m-%d")
            elif pd.isna(value):
                record[column] = None
            else:
                number = _clean_number(value)
                record[column] = number if number is not None else value
        records.append(record)
    return records


def _extract_estimate_table(table: Optional[pd.DataFrame], aliases: dict[str, list[str]]) -> dict:
    if table is None or table.empty:
        return {}
    normalized = {str(index).strip().lower(): index for index in table.index}
    result = {}
    for canonical, names in aliases.items():
        for name in names:
            key = name.strip().lower()
            if key not in normalized:
                continue
            row = table.loc[normalized[key]]
            values = {}
            for column, value in row.items():
                number = _clean_number(value)
                values[str(column)] = number if number is not None else None
            result[canonical] = values
            break
    return result


# ---------------------------------------------------------------------------
# Análisis principal
# ---------------------------------------------------------------------------

def analyze_current_earnings(ticker: str) -> dict:
    symbol = ticker.upper().strip()
    stock = yf.Ticker(symbol)

    try:
        income = stock.quarterly_income_stmt
        income_error = None
    except Exception as exc:
        income = pd.DataFrame()
        income_error = str(exc)

    yf_eps = _series_to_quarters(
        _extract_row(income, ["Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"])
    )
    yf_revenue = _series_to_quarters(
        _extract_row(income, ["Total Revenue", "Operating Revenue"])
    )
    yf_net_income = _series_to_quarters(
        _extract_row(income, ["Net Income", "Net Income Common Stockholders"])
    )

    yahoo_ts, timeseries_error = _fetch_yahoo_timeseries(
        symbol,
        ["quarterlyDilutedEPS", "quarterlyTotalRevenue", "quarterlyNetIncome"],
        years=5,
    )

    yahoo_eps = _merge_primary_with_fallback(
        yahoo_ts.get("quarterlyDilutedEPS", pd.Series(dtype="float64")), yf_eps
    )
    yahoo_revenue = _merge_primary_with_fallback(
        yahoo_ts.get("quarterlyTotalRevenue", pd.Series(dtype="float64")), yf_revenue
    )
    yahoo_net_income = _merge_primary_with_fallback(
        yahoo_ts.get("quarterlyNetIncome", pd.Series(dtype="float64")), yf_net_income
    )

    sec_data, sec_tags, sec_error = _fetch_sec_companyfacts(symbol, years=6)
    sec_eps = sec_data.get("EPS_DILUTED", pd.Series(dtype="float64"))
    sec_revenue = sec_data.get("REVENUE", pd.Series(dtype="float64"))
    sec_net_income = sec_data.get("NET_INCOME", pd.Series(dtype="float64"))

    eps = _merge_primary_with_fallback(sec_eps, yahoo_eps)
    revenue = _merge_primary_with_fallback(sec_revenue, yahoo_revenue)
    net_income = _merge_primary_with_fallback(sec_net_income, yahoo_net_income)

    sec_eps_growth = _growth_yoy_by_date(sec_eps)
    yahoo_eps_growth = _growth_yoy_by_date(yahoo_eps)
    eps_growth = _merge_growth_primary_with_fallback(
        sec_eps, sec_eps_growth, yahoo_eps, yahoo_eps_growth
    )

    sec_revenue_growth = _growth_yoy_by_date(sec_revenue)
    yahoo_revenue_growth = _growth_yoy_by_date(yahoo_revenue)
    revenue_growth = _merge_growth_primary_with_fallback(
        sec_revenue, sec_revenue_growth, yahoo_revenue, yahoo_revenue_growth
    )

    latest_eps, previous_eps_comparable, latest_eps_source = _latest_comparable_value(
        sec_eps, yahoo_eps
    )
    latest_revenue = _latest_valid(revenue)

    eps_loss_to_profit = (
        latest_eps is not None
        and previous_eps_comparable is not None
        and previous_eps_comparable <= 0
        and latest_eps > 0
    )

    surprises = []
    try:
        surprises = _history_records(stock.earnings_history, n=8)
    except Exception:
        pass

    surprise_values = []
    for record in surprises:
        value = _clean_number(record.get("surprisePercent"))
        if value is not None:
            surprise_values.append(value * 100.0)
    latest_surprise = surprise_values[-1] if surprise_values else None
    positive_surprises = sum(1 for value in surprise_values if value > 0)

    estimates, revisions, growth_estimates = {}, {}, {}
    try:
        estimates = _extract_estimate_table(
            stock.eps_estimate,
            {
                "current_qtr": ["Current Qtr.", "0q"],
                "next_qtr": ["Next Qtr.", "+1q"],
                "current_year": ["Current Year", "0y"],
                "next_year": ["Next Year", "+1y"],
            },
        )
    except Exception:
        pass
    try:
        revisions = _extract_estimate_table(
            stock.eps_revisions,
            {
                "up_7d": ["Up Last 7 Days"],
                "up_30d": ["Up Last 30 Days"],
                "down_7d": ["Down Last 7 Days"],
                "down_30d": ["Down Last 30 Days"],
            },
        )
    except Exception:
        pass
    try:
        growth_estimates = _extract_estimate_table(
            stock.growth_estimates,
            {
                "current_qtr": ["Current Qtr.", "0q"],
                "next_qtr": ["Next Qtr.", "+1q"],
                "current_year": ["Current Year", "0y"],
                "next_year": ["Next Year", "+1y"],
            },
        )
    except Exception:
        pass

    latest_eps_yoy = _latest_valid(eps_growth)
    previous_eps_yoy = _consecutive_previous(eps_growth)
    eps_acceleration = _growth_acceleration(eps_growth)
    latest_revenue_yoy = _latest_valid(revenue_growth)
    previous_revenue_yoy = _consecutive_previous(revenue_growth)
    revenue_acceleration = _growth_acceleration(revenue_growth)

    if all(v is not None for v in [
        latest_eps_yoy, previous_eps_yoy, eps_acceleration,
        latest_revenue_yoy, previous_revenue_yoy, revenue_acceleration,
    ]):
        data_quality = "completa"
    elif latest_eps_yoy is not None and latest_revenue_yoy is not None:
        data_quality = "suficiente"
    elif latest_eps_yoy is not None or latest_revenue_yoy is not None:
        data_quality = "parcial"
    else:
        data_quality = "insuficiente"

    if eps_loss_to_profit:
        eps_change_type = "perdidas_a_beneficios"
    elif latest_eps is None:
        eps_change_type = "sin_datos"
    else:
        eps_change_type = "normal"

    return {
        "ticker": symbol,
        "version": "2.4",
        "data_source": "SEC Company Facts + Yahoo Finance/yfinance",
        "error": income_error,
        "timeseries_error": timeseries_error,
        "sec_error": sec_error,
        "sec_tags": sec_tags,
        "eps_quarters": _last_values(eps, 16),
        "revenue_quarters": _last_values(revenue, 16),
        "net_income_quarters": _last_values(net_income, 16),
        "eps_yoy_pct": _last_values(eps_growth.dropna(), 12),
        "revenue_yoy_pct": _last_values(revenue_growth.dropna(), 12),
        "latest_eps": latest_eps,
        "latest_eps_source": latest_eps_source,
        "latest_eps_yoy_pct": latest_eps_yoy,
        "previous_eps_yoy_pct": previous_eps_yoy,
        "eps_acceleration_pp": eps_acceleration,
        "latest_revenue": latest_revenue,
        "latest_revenue_yoy_pct": latest_revenue_yoy,
        "previous_revenue_yoy_pct": previous_revenue_yoy,
        "revenue_acceleration_pp": revenue_acceleration,
        "latest_eps_positive": latest_eps is not None and latest_eps > 0,
        "eps_loss_to_profit": eps_loss_to_profit,
        "eps_change_type": eps_change_type,
        "earnings_surprises": surprises,
        "latest_eps_surprise_pct": latest_surprise,
        "positive_surprises_count": positive_surprises,
        "surprises_available": len(surprise_values),
        "eps_estimates": estimates,
        "eps_revisions": revisions,
        "growth_estimates": growth_estimates,
        "quarters_eps_available": len(eps),
        "quarters_revenue_available": len(revenue),
        "eps_yoy_calculable": int(eps_growth.dropna().shape[0]),
        "revenue_yoy_calculable": int(revenue_growth.dropna().shape[0]),
        "sec_eps_quarters": len(sec_eps),
        "sec_revenue_quarters": len(sec_revenue),
        "yahoo_eps_quarters": len(yahoo_eps),
        "yahoo_revenue_quarters": len(yahoo_revenue),
        "data_quality": data_quality,
    }


# ---------------------------------------------------------------------------
# Informe de consola
# ---------------------------------------------------------------------------

def _fmt(value, suffix="") -> str:
    if value is None:
        return "N/D"
    return f"{value:.2f}{suffix}"


def print_report(report: dict) -> None:
    print(f"\n{'=' * 72}")
    print(f"CAN SLIM+ v{report['version']} | C — {report['ticker']}")
    print(f"{'=' * 72}")

    if report.get("error"):
        print(f"Aviso yfinance: {report['error']}")
    if report.get("timeseries_error"):
        print(f"Aviso Yahoo TS: {report['timeseries_error']}")
    if report.get("sec_error"):
        print(f"Aviso SEC: {report['sec_error']}")

    print("\n[EPS]")
    print(f"  Último EPS:             {_fmt(report['latest_eps'])}")
    print(f"  Fuente último EPS:      {report['latest_eps_source'] or 'N/D'}")
    print(f"  EPS YoY actual:         {_fmt(report['latest_eps_yoy_pct'], '%')}")
    print(f"  EPS YoY anterior:       {_fmt(report['previous_eps_yoy_pct'], '%')}")
    print(f"  Aceleración EPS:        {_fmt(report['eps_acceleration_pp'], ' pp')}")
    print(f"  EPS positivo:           {report['latest_eps_positive']}")
    print(f"  Cambio pérdidas→benef.: {report['eps_loss_to_profit']}")

    print("\n[VENTAS]")
    print(f"  Ventas YoY actual:      {_fmt(report['latest_revenue_yoy_pct'], '%')}")
    print(f"  Ventas YoY anterior:    {_fmt(report['previous_revenue_yoy_pct'], '%')}")
    print(f"  Aceleración ventas:     {_fmt(report['revenue_acceleration_pp'], ' pp')}")

    print("\n[SORPRESA EPS]")
    print(f"  Última sorpresa:        {_fmt(report['latest_eps_surprise_pct'], '%')}")
    print(f"  Sorpresas positivas:    {report['positive_surprises_count']}/{report['surprises_available']}")

    print("\n[DATOS]")
    print(f"  SEC EPS / ventas:       {report['sec_eps_quarters']} / {report['sec_revenue_quarters']}")
    print(f"  Yahoo EPS / ventas:     {report['yahoo_eps_quarters']} / {report['yahoo_revenue_quarters']}")
    print(f"  Híbrido EPS / ventas:   {report['quarters_eps_available']} / {report['quarters_revenue_available']}")
    print(f"  YoY EPS calculables:    {report['eps_yoy_calculable']}")
    print(f"  YoY ventas calculables: {report['revenue_yoy_calculable']}")
    print(f"  Tags SEC:               {report['sec_tags']}")
    print(f"  Calidad:                {report['data_quality']}")

    print("\n[HISTÓRICO EPS YoY]")
    for item in report["eps_yoy_pct"]:
        print(f"  {item['date']}: {item['value']:+.2f}%")

    print("\n[HISTÓRICO VENTAS YoY]")
    for item in report["revenue_yoy_pct"]:
        print(f"  {item['date']}: {item['value']:+.2f}%")

    if report.get("growth_estimates"):
        print("\n[CRECIMIENTO ESTIMADO]")
        for name, values in report["growth_estimates"].items():
            print(f"  {name}: {values}")


if __name__ == "__main__":
    TEST_TICKERS = ["NVDA", "MSFT", "COST", "XOM", "AMZN"]
    for ticker in TEST_TICKERS:
        print_report(analyze_current_earnings(ticker))
