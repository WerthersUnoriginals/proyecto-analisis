"""
CAN SLIM+ — Módulo C: Current Earnings

Objetivo:
    Analizar el crecimiento reciente de EPS e ingresos de una acción usando
    datos trimestrales de Yahoo Finance mediante yfinance.

Este primer módulo NO decide comprar. Calcula métricas y devuelve un informe
transparente para que posteriormente fijemos los umbrales definitivos del
CAN SLIM+.

Métricas principales:
    - EPS trimestral y crecimiento YoY.
    - Aceleración/desaceleración del crecimiento EPS.
    - Ingresos trimestrales y crecimiento YoY.
    - Aceleración/desaceleración de ventas.
    - Sorpresas de EPS frente al consenso cuando Yahoo las proporciona.

Nota:
    Yahoo Finance puede presentar series incompletas o cambios de definición.
    El módulo no inventa datos: cuando no puede calcular una métrica devuelve
    None.

Requiere:
    pip install yfinance pandas
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd
import yfinance as yf


def _clean_number(value) -> Optional[float]:
    """Convierte un valor numérico de Yahoo a float o devuelve None."""
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _extract_row(df: pd.DataFrame, possible_names: list[str]) -> Optional[pd.Series]:
    """Busca una fila del estado financiero tolerando nombres de Yahoo."""
    if df is None or df.empty:
        return None

    normalized = {str(index).strip().lower(): index for index in df.index}
    for name in possible_names:
        key = name.strip().lower()
        if key in normalized:
            return df.loc[normalized[key]]
    return None


def _series_to_quarters(row: Optional[pd.Series]) -> pd.Series:
    """Normaliza una fila financiera a fechas ordenadas cronológicamente."""
    if row is None:
        return pd.Series(dtype="float64")

    values = []
    for date, value in row.items():
        number = _clean_number(value)
        if number is not None:
            values.append((pd.Timestamp(date), number))

    if not values:
        return pd.Series(dtype="float64")

    return pd.Series(dict(sorted(values)), dtype="float64")


def _growth_yoy(series: pd.Series) -> pd.Series:
    """Crecimiento interanual usando cuatro observaciones trimestrales."""
    if series.empty:
        return pd.Series(dtype="float64")

    result = pd.Series(index=series.index, dtype="float64")
    for i, date in enumerate(series.index):
        if i < 4:
            result.loc[date] = float("nan")
            continue

        current = series.iloc[i]
        previous = series.iloc[i - 4]

        # Si el denominador es cero o negativo, el porcentaje estándar puede
        # resultar engañoso. Lo dejamos como NaN para tratarlo explícitamente.
        if previous <= 0:
            result.loc[date] = float("nan")
        else:
            result.loc[date] = (current / previous - 1.0) * 100.0

    return result


def _acceleration(growth: pd.Series) -> Optional[float]:
    """Cambio en puntos porcentuales entre el crecimiento actual y el previo."""
    valid = growth.dropna()
    if len(valid) < 2:
        return None
    return float(valid.iloc[-1] - valid.iloc[-2])


def _latest_growth(growth: pd.Series) -> Optional[float]:
    valid = growth.dropna()
    return None if valid.empty else float(valid.iloc[-1])


def _last_values(series: pd.Series, n: int = 6) -> list[dict]:
    """Devuelve las últimas observaciones en formato JSON-friendly."""
    output = []
    for date, value in series.tail(n).items():
        output.append({"date": date.strftime("%Y-%m-%d"), "value": float(value)})
    return output


def analyze_current_earnings(ticker: str) -> dict:
    """Analiza C para un ticker y devuelve un diccionario estructurado."""
    symbol = ticker.upper().strip()
    stock = yf.Ticker(symbol)

    try:
        income = stock.quarterly_income_stmt
    except Exception as exc:
        return {"ticker": symbol, "error": f"No se pudo obtener income statement: {exc}"}

    # Yahoo puede utilizar distintos nombres según compañía/periodo.
    eps_row = _extract_row(
        income,
        [
            "Diluted EPS",
            "Basic EPS",
            "DilutedEPS",
            "BasicEPS",
        ],
    )
    revenue_row = _extract_row(
        income,
        [
            "Total Revenue",
            "Operating Revenue",
        ],
    )
    net_income_row = _extract_row(
        income,
        [
            "Net Income",
            "Net Income Common Stockholders",
        ],
    )

    eps = _series_to_quarters(eps_row)
    revenue = _series_to_quarters(revenue_row)
    net_income = _series_to_quarters(net_income_row)

    eps_growth = _growth_yoy(eps)
    revenue_growth = _growth_yoy(revenue)

    result = {
        "ticker": symbol,
        "data_source": "Yahoo Finance / yfinance",
        "eps_quarters": _last_values(eps),
        "eps_yoy_pct": _last_values(eps_growth.dropna()),
        "revenue_quarters": _last_values(revenue),
        "revenue_yoy_pct": _last_values(revenue_growth.dropna()),
        "net_income_quarters": _last_values(net_income),
        "latest_eps_yoy_pct": _latest_growth(eps_growth),
        "previous_eps_yoy_pct": (
            float(eps_growth.dropna().iloc[-2])
            if len(eps_growth.dropna()) >= 2
            else None
        ),
        "eps_acceleration_pp": _acceleration(eps_growth),
        "latest_revenue_yoy_pct": _latest_growth(revenue_growth),
        "previous_revenue_yoy_pct": (
            float(revenue_growth.dropna().iloc[-2])
            if len(revenue_growth.dropna()) >= 2
            else None
        ),
        "revenue_acceleration_pp": _acceleration(revenue_growth),
        "latest_eps_positive": (
            bool(eps.iloc[-1] > 0) if not eps.empty else None
        ),
    }

    # Earnings history: útil como capa adicional, pero independiente del
    # cálculo de crecimiento YoY del estado financiero.
    try:
        history = stock.earnings_history
        if history is not None and not history.empty:
            columns = [
                c for c in ["epsEstimate", "epsActual", "epsDifference", "surprisePercent"]
                if c in history.columns
            ]
            result["earnings_surprises"] = history[columns].tail(6).reset_index().to_dict("records")
        else:
            result["earnings_surprises"] = []
    except Exception:
        result["earnings_surprises"] = []

    return result


def print_report(report: dict) -> None:
    """Imprime un informe legible para pruebas manuales."""
    print(f"\n=== CAN SLIM+ | C — {report['ticker']} ===")
    if report.get("error"):
        print(report["error"])
        return

    print(f"EPS YoY actual:      {report['latest_eps_yoy_pct']}")
    print(f"EPS YoY anterior:    {report['previous_eps_yoy_pct']}")
    print(f"Aceleración EPS:     {report['eps_acceleration_pp']} pp")
    print(f"Ventas YoY actual:   {report['latest_revenue_yoy_pct']}")
    print(f"Ventas YoY anterior: {report['previous_revenue_yoy_pct']}")
    print(f"Aceleración ventas:  {report['revenue_acceleration_pp']} pp")
    print(f"EPS actual positivo: {report['latest_eps_positive']}")
    print("\nÚltimos EPS:")
    for item in report["eps_quarters"]:
        print(f"  {item['date']}: {item['value']}")


if __name__ == "__main__":
    # Prueba rápida. Cambiar ticker si se desea.
    print_report(analyze_current_earnings("NVDA"))
