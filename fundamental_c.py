"""
CAN SLIM+ — Módulo C: Current Earnings v2.1

Analiza el crecimiento reciente de EPS e ingresos usando Yahoo Finance /
yfinance. Esta versión separa:

    1) EPS trimestral y crecimiento YoY.
    2) Ventas trimestrales y crecimiento YoY.
    3) Aceleración/desaceleración cuando existen trimestres consecutivos.
    4) Cambio de pérdidas a beneficios.
    5) Sorpresas de EPS frente al consenso.
    6) Estimaciones y revisiones de EPS cuando Yahoo las proporciona.
    7) Diagnóstico de calidad/completitud de los datos.

NO asigna todavía una puntuación CAN SLIM definitiva. Primero validamos
los datos y el comportamiento del módulo en distintas empresas.

Requiere:
    pip install yfinance pandas
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd
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
    df: pd.DataFrame,
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
            values.append((pd.Timestamp(date), number))

    if not values:
        return pd.Series(dtype="float64")

    return pd.Series(dict(sorted(values)), dtype="float64")


def _growth_yoy(series: pd.Series) -> pd.Series:
    """
    Calcula crecimiento interanual por trimestre.

    Se utiliza el trimestre situado cuatro observaciones atrás. Si el valor
    comparable del año anterior es <= 0, no se fabrica un porcentaje: ese
    caso se trata aparte como transición de pérdidas a beneficios cuando
    corresponda.
    """
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


def _growth_acceleration(growth: pd.Series) -> Optional[float]:
    """
    Calcula aceleración en puntos porcentuales sólo entre dos observaciones
    YoY consecutivas del histórico original.
    """
    if growth.empty:
        return None

    valid = growth.dropna()
    if len(valid) < 2:
        return None

    # Exigimos que las dos observaciones YoY pertenezcan a trimestres
    # consecutivos. Así evitamos comparar Q4 con Q2 si falta Q3.
    latest_date = valid.index[-1]
    previous_date = valid.index[-2]

    days_between = (latest_date - previous_date).days
    if days_between < 70 or days_between > 110:
        return None

    return float(valid.iloc[-1] - valid.iloc[-2])


def _latest_valid(series: pd.Series) -> Optional[float]:
    valid = series.dropna()
    return None if valid.empty else float(valid.iloc[-1])


def _previous_consecutive_valid(series: pd.Series) -> Optional[float]:
    if series.empty:
        return None

    valid = series.dropna()
    if len(valid) < 2:
        return None

    latest_date = valid.index[-1]
    previous_date = valid.index[-2]
    days_between = (latest_date - previous_date).days

    if days_between < 70 or days_between > 110:
        return None

    return float(valid.iloc[-2])


def _last_values(series: pd.Series, n: int = 8) -> list[dict]:
    """Devuelve las últimas observaciones en formato JSON-friendly."""
    output = []
    for date, value in series.tail(n).items():
        output.append({
            "date": date.strftime("%Y-%m-%d"),
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
    table: pd.DataFrame,
    preferred_rows: list[str],
) -> dict:
    """Extrae filas relevantes de una tabla de estimaciones de Yahoo."""
    if table is None or table.empty:
        return {}

    result = {}
    normalized = {str(index).strip().lower(): index for index in table.index}

    for wanted in preferred_rows:
        key = wanted.strip().lower()
        if key not in normalized:
            continue

        row = table.loc[normalized[key]]
        clean = {}
        for column, value in row.items():
            number = _clean_number(value)
            clean[str(column)] = number if number is not None else None
        result[wanted] = clean

    return result


# ---------------------------------------------------------------------------
# Análisis principal
# ---------------------------------------------------------------------------

def analyze_current_earnings(ticker: str) -> dict:
    """Analiza la C de CAN SLIM+ para un ticker."""
    symbol = ticker.upper().strip()
    stock = yf.Ticker(symbol)

    try:
        income = stock.quarterly_income_stmt
    except Exception as exc:
        return {
            "ticker": symbol,
            "data_source": "Yahoo Finance / yfinance",
            "error": f"No se pudo obtener quarterly_income_stmt: {exc}",
        }

    eps_row = _extract_row(
        income,
        ["Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"],
    )
    revenue_row = _extract_row(
        income,
        ["Total Revenue", "Operating Revenue"],
    )
    net_income_row = _extract_row(
        income,
        ["Net Income", "Net Income Common Stockholders"],
    )

    eps = _series_to_quarters(eps_row)
    revenue = _series_to_quarters(revenue_row)
    net_income = _series_to_quarters(net_income_row)

    eps_growth = _growth_yoy(eps)
    revenue_growth = _growth_yoy(revenue)

    latest_eps = float(eps.iloc[-1]) if not eps.empty else None
    latest_revenue = float(revenue.iloc[-1]) if not revenue.empty else None

    previous_eps_comparable = float(eps.iloc[-5]) if len(eps) >= 5 else None
    eps_loss_to_profit = (
        latest_eps is not None
        and previous_eps_comparable is not None
        and previous_eps_comparable <= 0
        and latest_eps > 0
    )

    # -----------------------------------------------------------------------
    # Earnings history / surprises
    # -----------------------------------------------------------------------
    surprises = []
    try:
        history = stock.earnings_history
        surprises = _history_records(history, n=8)
    except Exception:
        pass

    surprise_values = []
    for record in surprises:
        value = _clean_number(record.get("surprisePercent"))
        if value is not None:
            surprise_values.append(value)

    latest_surprise = surprise_values[-1] if surprise_values else None
    positive_surprises = sum(1 for value in surprise_values if value > 0)

    # -----------------------------------------------------------------------
    # Estimates / revisions
    # -----------------------------------------------------------------------
    estimates = {}
    revisions = {}
    growth_estimates = {}

    try:
        estimates = _extract_estimate_table(
            stock.eps_estimate,
            ["Current Qtr.", "Next Qtr.", "Current Year", "Next Year"],
        )
    except Exception:
        pass

    try:
        revisions = _extract_estimate_table(
            stock.eps_revisions,
            [
                "Up Last 7 Days",
                "Up Last 30 Days",
                "Down Last 7 Days",
                "Down Last 30 Days",
            ],
        )
    except Exception:
        pass

    try:
        growth_estimates = _extract_estimate_table(
            stock.growth_estimates,
            ["Current Qtr.", "Next Qtr.", "Current Year", "Next Year"],
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
        "data_source": "Yahoo Finance / yfinance",

        # Datos brutos.
        "eps_quarters": _last_values(eps),
        "revenue_quarters": _last_values(revenue),
        "net_income_quarters": _last_values(net_income),

        # Crecimiento histórico.
        "eps_yoy_pct": _last_values(eps_growth.dropna()),
        "revenue_yoy_pct": _last_values(revenue_growth.dropna()),

        # Métricas actuales.
        "latest_eps": latest_eps,
        "latest_eps_yoy_pct": _latest_valid(eps_growth),
        "previous_eps_yoy_pct": _previous_consecutive_valid(eps_growth),
        "eps_acceleration_pp": _growth_acceleration(eps_growth),
        "latest_revenue": latest_revenue,
        "latest_revenue_yoy_pct": _latest_valid(revenue_growth),
        "previous_revenue_yoy_pct": _previous_consecutive_valid(revenue_growth),
        "revenue_acceleration_pp": _growth_acceleration(revenue_growth),

        # Casos especiales.
        "latest_eps_positive": latest_eps is not None and latest_eps > 0,
        "eps_loss_to_profit": eps_loss_to_profit,
        "eps_change_type": eps_change_type,

        # Sorpresas.
        "earnings_surprises": surprises,
        "latest_eps_surprise_pct": latest_surprise,
        "positive_surprises_count": positive_surprises,
        "surprises_available": len(surprise_values),

        # Futuro / consenso.
        "eps_estimates": estimates,
        "eps_revisions": revisions,
        "growth_estimates": growth_estimates,

        # Calidad de datos.
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
        print(report["error"])
        return

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

    print("\n[HISTÓRICO EPS]")
    for item in report["eps_yoy_pct"]:
        print(f"  {item['date']}: {item['value']:+.2f}%")

    print("\n[HISTÓRICO VENTAS]")
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


if __name__ == "__main__":
    # Lista de prueba. Cambiar o ampliar según las pruebas que queramos hacer.
    TEST_TICKERS = ["NVDA", "MSFT", "COST", "XOM", "AMZN"]

    for ticker in TEST_TICKERS:
        print_report(analyze_current_earnings(ticker))
