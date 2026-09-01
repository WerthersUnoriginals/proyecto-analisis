"""
TEST HÍBRIDO SEC EDGAR + YAHOO — histórico trimestral para CAN SLIM C

Objetivo:
- Recuperar histórico trimestral desde SEC Company Facts.
- Recuperar los trimestres recientes desde yfinance/Yahoo.
- Comparar ambas fuentes donde se solapan.
- Construir una serie híbrida de hasta 12 trimestres, sin modificar todavía
  fundamental_c.py.

Requiere:
    pip install requests pandas yfinance
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
import yfinance as yf


TICKERS = ["NVDA", "MSFT", "COST", "XOM", "AMZN"]
YEARS = 6
HYBRID_QUARTERS = 12
DATE_TOLERANCE_DAYS = 50

CIK_MAP = {
    "NVDA": "0001045810",
    "MSFT": "0000789019",
    "COST": "0000909832",
    "XOM": "0000034088",
    "AMZN": "0001018724",
}

HEADERS = {
    "User-Agent": "CANSLIMResearch/0.2 educational-research",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

TAG_CANDIDATES = {
    "EPS_DILUTED": ["EarningsPerShareDiluted"],
    "REVENUE": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "NET_INCOME": ["NetIncomeLoss", "ProfitLoss"],
}

UNIT_PREFERENCE = {
    "EPS_DILUTED": ["USD/shares", "USD / shares"],
    "REVENUE": ["USD"],
    "NET_INCOME": ["USD"],
}

YAHOO_ROW_NAMES = {
    "EPS_DILUTED": ["Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"],
    "REVENUE": ["Total Revenue", "Operating Revenue"],
    "NET_INCOME": ["Net Income", "Net Income Common Stockholders"],
}

REL_TOLERANCE = {
    "EPS_DILUTED": 0.03,   # 3%
    "REVENUE": 0.01,       # 1%
    "NET_INCOME": 0.02,    # 2%
}


def _clean_number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _get_json(url: str) -> dict:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def _days_between(start: str, end: str) -> Optional[int]:
    try:
        return int((pd.Timestamp(end) - pd.Timestamp(start)).days)
    except Exception:
        return None


def _quarter_like_fact(item: dict) -> bool:
    if item.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
        return False
    start, end = item.get("start"), item.get("end")
    if not start or not end:
        return False
    duration = _days_between(start, end)
    return duration is not None and 70 <= duration <= 110


def _cutoff_date(years: int) -> pd.Timestamp:
    now = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    return now - pd.DateOffset(years=years)


def _extract_tag_series(facts: dict, tag: str, metric_name: str, years: int) -> pd.DataFrame:
    concept = facts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not concept:
        return pd.DataFrame()

    units = concept.get("units", {})
    selected_unit = next((u for u in UNIT_PREFERENCE[metric_name] if u in units), None)
    if selected_unit is None:
        return pd.DataFrame()

    cutoff = _cutoff_date(years)
    rows = []
    for item in units.get(selected_unit, []):
        if not _quarter_like_fact(item):
            continue
        end, val = item.get("end"), item.get("val")
        if end is None or val is None:
            continue
        try:
            end_ts = pd.Timestamp(end)
            start_ts = pd.Timestamp(item.get("start"))
        except Exception:
            continue
        if end_ts < cutoff:
            continue
        rows.append({
            "date": end_ts,
            "start": start_ts,
            "value": float(val),
            "filed": pd.Timestamp(item.get("filed")) if item.get("filed") else pd.NaT,
            "form": item.get("form"),
            "fy": item.get("fy"),
            "fp": item.get("fp"),
            "frame": item.get("frame"),
            "tag": tag,
            "unit": selected_unit,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values(["date", "filed"]).drop_duplicates(subset=["date"], keep="last")
    return df.sort_values("date").reset_index(drop=True)


def _best_series(facts: dict, metric_name: str, years: int) -> tuple[pd.DataFrame, Optional[str]]:
    best, best_tag = pd.DataFrame(), None
    for tag in TAG_CANDIDATES[metric_name]:
        df = _extract_tag_series(facts, tag, metric_name, years)
        if len(df) > len(best):
            best, best_tag = df, tag
    return best, best_tag


def _extract_yahoo_row(df: pd.DataFrame, names: list[str]) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    normalized = {str(idx).strip().lower(): idx for idx in df.index}
    for name in names:
        key = name.strip().lower()
        if key in normalized:
            row = df.loc[normalized[key]]
            values = []
            for date, value in row.items():
                number = _clean_number(value)
                if number is not None:
                    values.append((pd.Timestamp(date).tz_localize(None), number))
            if values:
                return pd.Series(dict(sorted(values)), dtype="float64")
    return pd.Series(dtype="float64")


def _get_yahoo_series(ticker: str) -> tuple[dict[str, pd.Series], Optional[str]]:
    try:
        income = yf.Ticker(ticker).quarterly_income_stmt
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"

    result = {}
    for metric, names in YAHOO_ROW_NAMES.items():
        result[metric] = _extract_yahoo_row(income, names)
    return result, None


def _nearest_sec(sec_df: pd.DataFrame, yahoo_date: pd.Timestamp) -> Optional[pd.Series]:
    if sec_df.empty:
        return None
    tmp = sec_df.copy()
    tmp["distance"] = (tmp["date"] - yahoo_date).abs().dt.days
    row = tmp.sort_values("distance").iloc[0]
    if int(row["distance"]) > DATE_TOLERANCE_DAYS:
        return None
    return row


def _compare_overlap(metric: str, sec_df: pd.DataFrame, yahoo_series: pd.Series) -> list[dict]:
    rows = []
    for ydate, yval in yahoo_series.items():
        sec_row = _nearest_sec(sec_df, pd.Timestamp(ydate))
        if sec_row is None:
            rows.append({
                "yahoo_date": pd.Timestamp(ydate), "sec_date": None,
                "yahoo": float(yval), "sec": None, "diff_pct": None,
                "status": "SOLO_YAHOO",
            })
            continue

        sval = float(sec_row["value"])
        denom = max(abs(sval), abs(float(yval)), 1e-12)
        diff_pct = abs(float(yval) - sval) / denom * 100.0
        status = "COINCIDEN" if diff_pct <= REL_TOLERANCE[metric] * 100 else "DISCREPANCIA"
        rows.append({
            "yahoo_date": pd.Timestamp(ydate), "sec_date": pd.Timestamp(sec_row["date"]),
            "yahoo": float(yval), "sec": sval, "diff_pct": diff_pct,
            "status": status,
        })
    return rows


def _build_hybrid(sec_df: pd.DataFrame, yahoo_series: pd.Series) -> pd.DataFrame:
    records = []
    used_sec_dates = set()

    # Yahoo tiene prioridad en la ventana reciente; si existe SEC cercano lo marcamos.
    for ydate, yval in yahoo_series.items():
        sec_row = _nearest_sec(sec_df, pd.Timestamp(ydate))
        if sec_row is not None:
            used_sec_dates.add(pd.Timestamp(sec_row["date"]))
        records.append({
            "date": pd.Timestamp(ydate),
            "value": float(yval),
            "source": "YAHOO+SEC" if sec_row is not None else "YAHOO",
        })

    # Añadimos SEC histórico que no quede representado por Yahoo.
    for _, row in sec_df.iterrows():
        sdate = pd.Timestamp(row["date"])
        if sdate in used_sec_dates:
            continue
        records.append({"date": sdate, "value": float(row["value"]), "source": "SEC"})

    if not records:
        return pd.DataFrame(columns=["date", "value", "source"])

    out = pd.DataFrame(records).sort_values("date")
    return out.tail(HYBRID_QUARTERS).reset_index(drop=True)


def _gap_report(hybrid: pd.DataFrame) -> list[int]:
    if len(hybrid) < 2:
        return []
    dates = hybrid["date"].tolist()
    return [int((dates[i] - dates[i - 1]).days) for i in range(1, len(dates))]


def main() -> None:
    print("=" * 86)
    print("TEST HÍBRIDO SEC + YAHOO — CAN SLIM C")
    print("=" * 86)
    print(f"Tickers: {', '.join(TICKERS)} | serie objetivo: {HYBRID_QUARTERS} trimestres")

    for ticker in TICKERS:
        print("\n" + "=" * 86)
        print(f"TICKER: {ticker}")
        print("=" * 86)

        cik = CIK_MAP[ticker]
        try:
            facts = _get_json(COMPANYFACTS_URL.format(cik=cik))
        except Exception as exc:
            print(f"ERROR SEC: {type(exc).__name__}: {exc}")
            continue

        yahoo, yahoo_error = _get_yahoo_series(ticker)
        if yahoo_error:
            print(f"AVISO YAHOO: {yahoo_error}")

        for metric in ["EPS_DILUTED", "REVENUE", "NET_INCOME"]:
            sec_df, tag = _best_series(facts, metric, YEARS)
            yser = yahoo.get(metric, pd.Series(dtype="float64"))
            overlap = _compare_overlap(metric, sec_df, yser)
            hybrid = _build_hybrid(sec_df, yser)
            gaps = _gap_report(hybrid)

            print(f"\n--- {metric} ---")
            print(f"SEC tag: {tag or 'N/D'} | SEC registros: {len(sec_df)} | Yahoo registros: {len(yser)}")
            print("SOLAPE SEC ↔ YAHOO:")
            if not overlap:
                print("  Sin solape disponible")
            else:
                for r in overlap:
                    sd = r["sec_date"].strftime("%Y-%m-%d") if r["sec_date"] is not None else "N/D"
                    diff = f"{r['diff_pct']:.2f}%" if r["diff_pct"] is not None else "N/D"
                    print(
                        f"  Yahoo {r['yahoo_date'].strftime('%Y-%m-%d')}={r['yahoo']} | "
                        f"SEC {sd}={r['sec']} | diff={diff} | {r['status']}"
                    )

            print(f"SERIE HÍBRIDA ({len(hybrid)} registros):")
            for _, row in hybrid.iterrows():
                print(f"  {row['date'].strftime('%Y-%m-%d')} | {row['value']} | {row['source']}")

            if gaps:
                large_gaps = [g for g in gaps if g > 140]
                print(f"GAPS días: {gaps}")
                print(f"GAPS >140 días: {large_gaps if large_gaps else 'ninguno'}")

            counts = {}
            for r in overlap:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            print(f"RESUMEN SOLAPE: {counts}")

        time.sleep(0.25)

    print("\n" + "=" * 86)
    print("FIN DEL TEST")
    print("=" * 86)


if __name__ == "__main__":
    main()
