"""
CAN SLIM+ — Test Yahoo Fundamentals Time Series

Objetivo:
    Comprobar si Yahoo Finance permite recuperar más de los 5 trimestres que
    devuelve actualmente una consulta amplia del endpoint Fundamentals Time
    Series.

Estrategia:
    En lugar de pedir 5 años de una sola vez, dividimos el periodo en bloques
    de ~1 año. Yahoo parece limitar algunas respuestas amplias a 5 registros.
    Los bloques se solapan ligeramente y posteriormente se eliminan duplicados.

Este archivo NO modifica todavía la lógica de fundamental_c.py.
Si funciona, trasladaremos esta estrategia a la función de producción.

Requiere:
    pip install requests pandas
"""

from __future__ import annotations

import math
import time
from typing import Optional

import pandas as pd
import requests


TICKERS = ["NVDA", "MSFT", "COST", "XOM", "AMZN"]
SERIES = [
    "quarterlyDilutedEPS",
    "quarterlyTotalRevenue",
    "quarterlyNetIncome",
]

YEARS = 5
CHUNK_DAYS = 370
OVERLAP_DAYS = 35


def _clean_number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _timestamp_to_epoch(timestamp: pd.Timestamp) -> int:
    return int(timestamp.timestamp())


def _fetch_chunk(
    session: requests.Session,
    ticker: str,
    series_type: str,
    period1: int,
    period2: int,
) -> list[tuple[pd.Timestamp, float]]:
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
        "merge": "false",
        "lang": "en-US",
        "region": "US",
    }

    response = session.get(url, params=params, timeout=15)
    response.raise_for_status()
    payload = response.json()

    rows: list[tuple[pd.Timestamp, float]] = []
    results = payload.get("timeseries", {}).get("result", [])

    for item in results:
        for value in item.get(series_type, []):
            date = value.get("asOfDate")
            raw = value.get("reportedValue", {}).get("raw")
            number = _clean_number(raw)
            if date is None or number is None:
                continue
            try:
                timestamp = pd.Timestamp(date).tz_localize(None)
            except (TypeError, ValueError):
                timestamp = pd.Timestamp(date)
            rows.append((timestamp, number))

    return rows


def fetch_series_chunked(
    session: requests.Session,
    ticker: str,
    series_type: str,
    years: int = YEARS,
) -> tuple[pd.Series, int, list[str]]:
    """Descarga una serie por bloques para evitar la limitación de respuestas amplias."""
    end = pd.Timestamp.now(tz="UTC").tz_localize(None) + pd.Timedelta(days=2)
    start = end - pd.Timedelta(days=years * 366)

    all_rows: list[tuple[pd.Timestamp, float]] = []
    errors: list[str] = []
    requests_count = 0

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(
            chunk_start + pd.Timedelta(days=CHUNK_DAYS),
            end,
        )

        # Solape para evitar perder un trimestre situado justo en el borde.
        query_start = chunk_start
        if chunk_start > start:
            query_start = chunk_start - pd.Timedelta(days=OVERLAP_DAYS)

        period1 = _timestamp_to_epoch(query_start)
        period2 = _timestamp_to_epoch(chunk_end)
        requests_count += 1

        try:
            rows = _fetch_chunk(
                session,
                ticker,
                series_type,
                period1,
                period2,
            )
            all_rows.extend(rows)
        except (requests.RequestException, ValueError) as exc:
            errors.append(
                f"bloque {query_start.date()} → {chunk_end.date()}: {exc}"
            )

        chunk_start = chunk_end

    if not all_rows:
        return pd.Series(dtype="float64"), requests_count, errors

    # Deduplicación por fecha. Si Yahoo entrega dos valores para una misma
    # fecha por el solape, conservamos uno solo.
    data = {}
    for timestamp, value in all_rows:
        data[timestamp] = value

    series = pd.Series(data, dtype="float64").sort_index()
    return series, requests_count, errors


def print_series(name: str, series: pd.Series) -> None:
    print(f"\n--- {name} ---")
    print(f"REGISTROS FINALES: {len(series)}")
    for date, value in series.items():
        print(f"  {date.strftime('%Y-%m-%d')} -> {value}")


def main() -> None:
    print("=" * 70)
    print("TEST YAHOO FINANCE — HISTÓRICO POR BLOQUES")
    print("=" * 70)
    print(f"Periodo: últimos ~{YEARS} años")
    print(f"Bloques: ~{CHUNK_DAYS} días con solape de {OVERLAP_DAYS} días")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
    )

    for ticker in TICKERS:
        print("\n" + "=" * 70)
        print(f"TICKER: {ticker}")
        print("=" * 70)

        for series_type in SERIES:
            try:
                series, request_count, errors = fetch_series_chunked(
                    session,
                    ticker,
                    series_type,
                )
            except Exception as exc:
                print(f"\n--- {series_type} ---")
                print(f"ERROR: {exc}")
                continue

            print(f"\n{series_type}")
            print(f"PETICIONES REALIZADAS: {request_count}")
            print(f"REGISTROS RECUPERADOS: {len(series)}")

            if errors:
                print("AVISOS:")
                for error in errors:
                    print(f"  - {error}")

            print_series(series_type, series)

        # Pequeña pausa para no martillear el endpoint.
        time.sleep(0.5)

    print("\n" + "=" * 70)
    print("FIN DEL TEST")
    print("=" * 70)
    print("Si aparecen ~20 trimestres por serie, trasladaremos esta estrategia")
    print("a fundamental_c.py y podremos calcular correctamente el histórico YoY.")


if __name__ == "__main__":
    main()
