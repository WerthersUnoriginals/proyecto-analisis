"""
TEST SEC EDGAR / XBRL — histórico trimestral para CAN SLIM C

Objetivo:
- Recuperar 12-20+ trimestres de EPS diluido, ventas e ingreso neto
  desde SEC Company Facts.
- No modifica fundamental_c.py.

Requiere:
    pip install requests pandas
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests


TICKERS = ["NVDA", "MSFT", "COST", "XOM", "AMZN"]
YEARS = 6

# Para este test evitamos una segunda llamada a www.sec.gov sólo para resolver
# ticker→CIK. Los CIK son identificadores públicos y estables de la SEC.
CIK_MAP = {
    "NVDA": "0001045810",
    "MSFT": "0000789019",
    "COST": "0000909832",
    "XOM": "0000034088",
    "AMZN": "0001018724",
}

HEADERS = {
    "User-Agent": "CANSLIMResearch/0.1 educational-research",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

TAG_CANDIDATES = {
    "EPS_DILUTED": [
        "EarningsPerShareDiluted",
    ],
    "REVENUE": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "NET_INCOME": [
        "NetIncomeLoss",
        "ProfitLoss",
    ],
}

UNIT_PREFERENCE = {
    "EPS_DILUTED": ["USD/shares", "USD / shares"],
    "REVENUE": ["USD"],
    "NET_INCOME": ["USD"],
}


def _get_json(url: str) -> dict:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def _days_between(start: str, end: str) -> Optional[int]:
    try:
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return int((e - s).days)
    except Exception:
        return None


def _quarter_like_fact(item: dict) -> bool:
    form = item.get("form")
    if form not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
        return False

    start = item.get("start")
    end = item.get("end")
    if not start or not end:
        return False

    duration = _days_between(start, end)
    if duration is None:
        return False

    # Aproximación a un trimestre real. Incluye compañías con 13 semanas.
    return 70 <= duration <= 110


def _cutoff_date(years: int) -> pd.Timestamp:
    now = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    return now - pd.DateOffset(years=years)


def _extract_tag_series(facts: dict, tag: str, metric_name: str, years: int) -> pd.DataFrame:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    concept = us_gaap.get(tag)
    if not concept:
        return pd.DataFrame()

    units = concept.get("units", {})
    selected_unit = None
    for preferred in UNIT_PREFERENCE[metric_name]:
        if preferred in units:
            selected_unit = preferred
            break

    if selected_unit is None:
        return pd.DataFrame()

    cutoff = _cutoff_date(years)
    rows = []

    for item in units.get(selected_unit, []):
        if not _quarter_like_fact(item):
            continue

        end = item.get("end")
        val = item.get("val")
        if end is None or val is None:
            continue

        try:
            end_ts = pd.Timestamp(end)
        except Exception:
            continue

        if end_ts < cutoff:
            continue

        rows.append(
            {
                "end": end_ts,
                "start": pd.Timestamp(item.get("start")),
                "value": float(val),
                "filed": pd.Timestamp(item.get("filed")) if item.get("filed") else pd.NaT,
                "form": item.get("form"),
                "fy": item.get("fy"),
                "fp": item.get("fp"),
                "frame": item.get("frame"),
                "accn": item.get("accn"),
                "tag": tag,
                "unit": selected_unit,
                "duration_days": _days_between(item.get("start"), item.get("end")),
            }
        )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Para un mismo cierre pueden aparecer versiones repetidas en filings posteriores.
    # Priorizamos el hecho presentado más recientemente.
    df = df.sort_values(["end", "filed"]).drop_duplicates(subset=["end"], keep="last")
    return df.sort_values("end").reset_index(drop=True)


def _best_series(facts: dict, metric_name: str, years: int) -> tuple[pd.DataFrame, Optional[str]]:
    best = pd.DataFrame()
    best_tag = None

    for tag in TAG_CANDIDATES[metric_name]:
        df = _extract_tag_series(facts, tag, metric_name, years)
        if len(df) > len(best):
            best = df
            best_tag = tag

    return best, best_tag


def print_metric(metric_name: str, df: pd.DataFrame, tag: Optional[str]) -> None:
    print(f"\n--- {metric_name} ---")
    print(f"TAG ELEGIDO: {tag or 'N/D'}")
    print(f"REGISTROS TRIMESTRALES: {len(df)}")

    if df.empty:
        return

    for _, row in df.iterrows():
        start = row["start"].strftime("%Y-%m-%d")
        end = row["end"].strftime("%Y-%m-%d")
        value = row["value"]
        form = row["form"]
        fp = row["fp"]
        frame = row["frame"]
        print(
            f"  {start} -> {end} | {value} | "
            f"form={form} fp={fp} frame={frame}"
        )


def main() -> None:
    print("=" * 72)
    print("TEST SEC EDGAR / XBRL — HISTÓRICO TRIMESTRAL")
    print("=" * 72)
    print(f"Tickers: {', '.join(TICKERS)}")
    print(f"Ventana: últimos ~{YEARS} años")
    print("Fuente: SEC Company Facts")

    for ticker in TICKERS:
        print("\n" + "=" * 72)
        print(f"TICKER: {ticker}")
        print("=" * 72)

        cik = CIK_MAP.get(ticker)
        if not cik:
            print("CIK no encontrado")
            continue

        print(f"CIK: {cik}")

        try:
            facts = _get_json(COMPANYFACTS_URL.format(cik=cik))
        except Exception as exc:
            print(f"ERROR SEC Company Facts: {type(exc).__name__}: {exc}")
            continue

        entity_name = facts.get("entityName")
        if entity_name:
            print(f"Entidad SEC: {entity_name}")

        for metric in ["EPS_DILUTED", "REVENUE", "NET_INCOME"]:
            df, tag = _best_series(facts, metric, YEARS)
            print_metric(metric, df, tag)

        time.sleep(0.25)

    print("\n" + "=" * 72)
    print("FIN DEL TEST")
    print("=" * 72)
    print(
        "Objetivo: confirmar si SEC permite recuperar al menos 12-20 "
        "trimestres fiables por métrica."
    )


if __name__ == "__main__":
    main()
