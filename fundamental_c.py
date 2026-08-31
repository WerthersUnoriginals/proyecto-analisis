"""
CAN SLIM+ — Módulo C: Current Earnings v2.2

Obtiene y analiza crecimiento trimestral de EPS y ventas para la parte C de
CAN SLIM. Usa yfinance como fuente principal y el endpoint público de Yahoo
Finance Fundamentals Time Series para ampliar el histórico trimestral cuando
Yahoo limita quarterly_income_stmt a pocas observaciones.

Esta versión NO asigna todavía un score CAN SLIM definitivo.

Requiere:
    pip install yfinance pandas requests
"""

from __future__ import annotations

import math
import time
from typing import Optional

import pandas as pd
import requests
import yfinance as yf


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _clean_number(value) -> Optional[float]:
    """Convierte un valor a float finito o devuelve None."""
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _extract_row(
    df: Optional[pd.DataFrame],
    possible_names: list[str],
) -> Optional[pd.Series]:
    """Busca una fila financiera tolerando variaciones de nombres de Yahoo."""
    if df is None or df.empty:
        return None

    normalized = {str(index).strip().lower(): index for index in df.index}
    for name in possible_names:
        key = name.strip().lower()
        if key in normalized:
            return df.loc[normalized[key]]
    return None


def _series_to_quarters(row: Optional[pd.Series]) -> pd.Series:
    """Convierte una fila de Yahoo en una serie cronológica limpia."""
    if row is None:
        return pd.Series(dtype="float64")

    values = []
    for date, value in row.items():
        number = _clean_number(value)
        if number is not None:
            try:
                timestamp = pd.Timestamp(date).tz_localize(None)
            except (TypeError, ValueError):
                timestamp = pd.Timestamp(date)
            values.append((timestamp, number))

    if not values:
        return pd.Series(dtype="float64")

    return pd.Series(dict(sorted(values)), dtype="float64")


def _merge_series(primary: pd.Series, secondary: pd.Series) -> pd.Series:
    """Combina dos series dando prioridad a primary en fechas duplicadas."""
    if primary.empty:
        return secondary.copy()
    if secondary.empty:
        return primary.copy()

    merged = pd.concat([secondary, primary])
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged.sort_index()


def _fetch_yahoo_timeseries(
    ticker: str,
    series_types: list[str],
    years: int = 5,
) -> dict[str, pd.Series]:
    """
    Descarga series trimestrales del endpoint Fundamentals Time Series de Yahoo.

    Se utiliza como complemento de yfinance porque quarterly_income_stmt puede
    devolver únicamente unas pocas observaciones recientes.
    """
    now = int(time.time())
    period1 = now - years * 366 * 24 * 60 * 60
    period2 = now + 24 * 60 * 60

    url = (
        "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/"
        f"finance/timeseries/{ticker}"
    )
    params = {
        "type": ",".join(series_types),
        "period1": period1,
        "period2": period2,
    }
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return {}

    result: dict[str, pd.Series] = {}
    results = payload.get("timeseries", {}).get("result", [])

    for item in results:
        key = item.get("meta", {}).get("type", [None])[0]
        if not key:
            # Algunas respuestas no incluyen meta.type; recuperamos el nombre
            # a partir de la única clave que contiene valores.
            candidates = [
                name for name in series_types
                if name in item
            ]
            key = candidates[0] if candidates else None

        if not key:
            continue

        rows = []
        for value in item.get(key, []):
            date = value.get("asOfDate")
            number = _clean_number(value.get("reportedValue", {}).get("raw"))
            if date is not None and number is not None:
                rows.append((pd.Timestamp(date), number))

        if rows:
            result[key] = pd.Series(dict(sorted(rows)), dtype="float64")

    return result


def _growth_yoy(series: pd.Series) -> pd.Series:
    """Calcula crecimiento YoY por trimestre; no fabrica porcentajes con base <= 0."""
    if series.empty:
        return pd.Series(dtype="float64")

    result = pd.Series(index=series.index, dtype="float64")

    for i, date in enumerate(series.index):
        if i < 4:
            result.loc[date] = float("nan")
            continue

        current = float(series.iloc[i])
        previous = float(series.iloc[i - 4])

        if previous <= 0:
            result.loc[date] = float("nan")
        else:
            result.loc[date] = (current / previous - 1.0) * 100.0

    return result


def _consecutive_previous(growth: pd.Series) -> Optional[float]:
    """Devuelve el YoY anterior sólo si corresponde al trimestre anterior."""
    valid = growth.dropna()
    if len(valid) < 2:
        return None

    latest_date = valid.index[-1]
    previous_date = valid.index[-2]
    days_between = (latest_date - previous_date).days

    if not 70 <= days_between <= 110:
        return None

    return float(valid.iloc[-2])


def _growth_acceleration(growth: pd.Series) -> Optional[float]:
    """Calcula aceleración YoY en puntos porcentuales entre trimestres consecutivos."""
    previous = _consecutive_previous(growth)
    latest = _latest_valid(growth)
    if previous is None or latest is None:
        return None
    return latest - previous


def _latest_valid(series: pd.Series) -> Optional[float]:
    valid = series.dropna()
    return None if valid.empty else float(valid.iloc[-1])


def _last_values(series: pd.Series, n: int = 12) -> list[dict]:
    """Devuelve observaciones recientes en formato sencillo."""
    output = []
    for date, value in series.tail(n).items():
        output.append({
            "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
            "value": float(value),
        })
    return output


def _history_records(history: pd.DataFrame, n: int = 8) -> list[dict]:
    """Normaliza earnings_history para impresión y posterior procesamiento."""
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


def _extract_estimate_table(
    table: Optional[pd.DataFrame],
    aliases: dict[str, list[str]],
) -> dict:
    """Extrae estimaciones tolerando etiquetas distintas de yfinance."""
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
    """Analiza la C de CAN SLIM+ para un ticker."""
    symbol = ticker.upper().strip()
    stock = yf.Ticker(symbol)

    # Primero intentamos obtener el estado trimestral mediante yfinance.
    try:
        income = stock.quarterly_income_stmt
    except Exception as exc:
        income = pd.DataFrame()
        income_error = str(exc)
    else:
        income_error = None

    eps = _series_to_quarters(
        _extract_row(income, ["Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"])
    )
    revenue = _series_to_quarters(
        _extract_row(income, ["Total Revenue", "Operating Revenue"])
    )
    net_income = _series_to_quarters(
        _extract_row(income, ["Net Income", "Net Income Common Stockholders"])
    )

    # Complementamos con Fundamentals Time Series para ampliar el histórico.
    timeseries = _fetch_yahoo_timeseries(
        symbol,
        ["quarterlyDilutedEPS", "quarterlyTotalRevenue", "quarterlyNetIncome"],
        years=5,
    )
    eps = _merge_series(eps, timeseries.get("quarterlyDilutedEPS", pd.Series(dtype="float64")))
    revenue = _merge_series(revenue, timeseries.get("quarterlyTotalRevenue", pd.Series(dtype="float64")))
    net_income = _merge_series(net_income, timeseries.get("quarterlyNetIncome", pd.Series(dtype="float64")))

    eps_growth = _growth_yoy(eps)
    revenue_growth = _growth_yoy(revenue)

    latest_eps = _latest_valid(eps)
    latest_revenue = _latest_valid(revenue)

    previous_eps_comparable = float(eps.iloc[-5]) if len(eps) >= 5 else None
    eps_loss_to_profit = (
        latest_eps is not None
        and previous_eps_comparable is not None
        and previous_eps_comparable <= 0
        and latest_eps > 0
    )

    # -----------------------------------------------------------------------
    # Sorpresas de EPS
    # -----------------------------------------------------------------------
    surprises = []
    try:
        surprises = _history_records(stock.earnings_history, n=8)
    except Exception:
        pass

    surprise_values = []
    for record in surprises:
        value = _clean_number(record.get("surprisePercent"))
        if value is not None:
            # Yahoo expresa surprisePercent como decimal (0.06 = 6%).
            surprise_values.append(value * 100.0)

    latest_surprise = surprise_values[-1] if surprise_values else None
    positive_surprises = sum(1 for value in surprise_values if value > 0)

    # -----------------------------------------------------------------------
    # Estimaciones y revisiones
    # -----------------------------------------------------------------------
    estimates = {}
    revisions = {}
    growth_estimates = {}

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
                "up_7d": ["Up Last 7 Days", "0q"],
                "up_30d": ["Up Last 30 Days", "+1q"],
                "down_7d": ["Down Last 7 Days", "0y"],
                "down_30d": ["Down Last 30 Days", "+1y"],
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

    # -----------------------------------------------------------------------
    # Diagnóstico de datos
    # -----------------------------------------------------------------------
    eps_yoy_count = int(eps_growth.dropna().shape[0])
    revenue_yoy_count = int(revenue_growth.dropna().shape[0])

    if eps_yoy_count >= 3 and revenue_yoy_count >= 3:
        data_quality = "suficiente"
    elif eps_yoy_count >= 1 and revenue_yoy_count >= 1:
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
        "data_source": "Yahoo Finance / yfinance + Fundamentals Time Series",
        "error": income_error,
        "eps_quarters": _last_values(eps),
        "revenue_quarters": _last_values(revenue),
        "net_income_quarters": _last_values(net_income),
        "eps_yoy_pct": _last_values(eps_growth.dropna()),
        "revenue_yoy_pct": _last_values(revenue_growth.dropna()),
        "latest_eps": latest_eps,
        "latest_eps_yoy_pct": _latest_valid(eps_growth),
        "previous_eps_yoy_pct": _consecutive_previous(eps_growth),
        "eps_acceleration_pp": _growth_acceleration(eps_growth),
        "latest_revenue": latest_revenue,
        "latest_revenue_yoy_pct": _latest_valid(revenue_growth),
        "previous_revenue_yoy_pct": _consecutive_previous(revenue_growth),
        "revenue_acceleration_pp": _growth_acceleration(revenue_growth),
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
        "eps_yoy_calculable": eps_yoy_count,
        "revenue_yoy_calculable": revenue_yoy_count,
        "data_quality": data_quality,
    }


# ---------------------------------------------------------------------------
# Informe de consola
# ---------------------------------------------------------------------------

def _fmt(value, suffix="") -> str:
    """Formatea una métrica numérica para la consola."""
    if value is None:
        return "N/D"
    return f"{value:.2f}{suffix}"


def print_report(report: dict) -> None:
    """Imprime el informe C de un ticker."""
    print(f"\n{'=' * 64}")
    print(f"CAN SLIM+ | C — {report['ticker']}")
    print(f"{'=' * 64}")

    if report.get("error"):
        print(f"Aviso yfinance: {report['error']}")

    print("\n[EPS]")
    print(f"  Último EPS:             {_fmt(report['latest_eps'])}")
    print(f"  EPS YoY actual:         {_fmt(report['latest_eps_yoy_pct'], '%')}")
    print(f"  EPS YoY anterior:       {_fmt(report['previous_eps_yoy_pct'], '%')}")
    print(f"  Aceleración EPS:        {_fmt(report['eps_acceleration_pp'], ' pp')}")
    print(f"  EPS positivo:           {report['latest_eps_positive']}")
    print(f"  Cambio pérdidas→benef.: {report['eps_loss_to_profit']}")
    print(f"  Tipo de cambio:         {report['eps_change_type']}")

    print("\n[VENTAS]")
    print(f"  Ventas YoY actual:      {_fmt(report['latest_revenue_yoy_pct'], '%')}")
    print(f"  Ventas YoY anterior:    {_fmt(report['previous_revenue_yoy_pct'], '%')}")
    print(f"  Aceleración ventas:     {_fmt(report['revenue_acceleration_pp'], ' pp')}")

    print("\n[SORPRESA EPS]")
    print(f"  Última sorpresa:        {_fmt(report['latest_eps_surprise_pct'], '%')}")
    print(
        f"  Sorpresas positivas:    "
        f"{report['positive_surprises_count']}/{report['surprises_available']}"
    )

    print("\n[DATOS]")
    print(f"  Trimestres EPS:         {report['quarters_eps_available']}")
    print(f"  YoY EPS calculables:    {report['eps_yoy_calculable']}")
    print(f"  Trimestres ventas:      {report['quarters_revenue_available']}")
    print(f"  YoY ventas calculables: {report['revenue_yoy_calculable']}")
    print(f"  Calidad:                {report['data_quality']}")

    print("\n[HISTÓRICO EPS YoY]")
    for item in report["eps_yoy_pct"]:
        print(f"  {item['date']}: {item['value']:+.2f}%")

    print("\n[HISTÓRICO VENTAS YoY]")
    for item in report["revenue_yoy_pct"]:
        print(f"  {item['date']}: {item['value']:+.2f}%")

    if report.get("eps_estimates"):
        print("\n[ESTIMACIONES EPS]")
        for name, values in report["eps_estimates"].items():
            print(f"  {name}: {values}")

    if report.get("eps_revisions"):
        print("\n[REVISIONES EPS]")
        for name, values in report["eps_revisions"].items():
            print(f"  {name}: {values}")

    if report.get("growth_estimates"):
        print("\n[CRECIMIENTO ESTIMADO]")
        for name, values in report["growth_estimates"].items():
            print(f"  {name}: {values}")


if __name__ == "__main__":
    TEST_TICKERS = ["NVDA", "MSFT", "COST", "XOM", "AMZN"]

    for ticker in TEST_TICKERS:
        print_report(analyze_current_earnings(ticker))
