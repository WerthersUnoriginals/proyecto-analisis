"""
TEST de splits para CAN SLIM+ C

Objetivo:
- Detectar splits con yfinance.
- Inspeccionar posibles conceptos SEC/XBRL relacionados con splits.
- Comparar EPS reportado, beneficio neto y diluted shares para ver si el EPS
  histórico de SEC parece ya restatado tras un split.

No modifica fundamental_c.py.

Requiere:
    pip install requests pandas yfinance
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd
import requests
import yfinance as yf


TICKERS = ["NVDA", "AMZN", "MSFT"]
CIK_MAP = {
    "NVDA": "0001045810",
    "AMZN": "0001018724",
    "MSFT": "0000789019",
}

HEADERS = {
    "User-Agent": "CANSLIMResearch/0.1 educational-research",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def clean_number(value) -> Optional[float]:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except Exception:
        return None


def get_sec_companyfacts(cik: str) -> dict:
    r = requests.get(COMPANYFACTS_URL.format(cik=cik), headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def quarterly_fact_series(facts: dict, tag: str, preferred_units: list[str], years: int = 6) -> pd.DataFrame:
    concept = facts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not concept:
        return pd.DataFrame()

    units = concept.get("units", {})
    unit = next((u for u in preferred_units if u in units), None)
    if unit is None:
        return pd.DataFrame()

    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.DateOffset(years=years)
    rows = []

    for item in units[unit]:
        if item.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
            continue
        start, end = item.get("start"), item.get("end")
        if not start or not end:
            continue
        try:
            start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        except Exception:
            continue
        duration = (end_ts - start_ts).days
        if not 70 <= duration <= 110:
            continue
        if end_ts < cutoff:
            continue
        val = clean_number(item.get("val"))
        if val is None:
            continue
        rows.append({
            "start": start_ts,
            "end": end_ts,
            "value": val,
            "filed": pd.Timestamp(item.get("filed")) if item.get("filed") else pd.NaT,
            "form": item.get("form"),
            "fp": item.get("fp"),
            "frame": item.get("frame"),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values(["end", "filed"]) \
        .drop_duplicates(subset=["end"], keep="last") \
        .sort_values("end") \
        .reset_index(drop=True)
    return df


def list_split_related_sec_tags(facts: dict) -> list[tuple[str, str]]:
    results = []
    for tag, concept in facts.get("facts", {}).get("us-gaap", {}).items():
        text = f"{tag} {concept.get('label','')} {concept.get('description','')}".lower()
        if "split" in text:
            results.append((tag, concept.get("label", "")))
    return sorted(results)


def get_yfinance_splits(ticker: str) -> pd.Series:
    try:
        s = yf.Ticker(ticker).splits
    except Exception:
        return pd.Series(dtype="float64")
    if s is None or len(s) == 0:
        return pd.Series(dtype="float64")
    s = s.copy()
    try:
        s.index = pd.to_datetime(s.index).tz_localize(None)
    except Exception:
        try:
            s.index = pd.to_datetime(s.index).tz_convert(None)
        except Exception:
            pass
    return s.sort_index()


def nearest_rows(df: pd.DataFrame, split_date: pd.Timestamp, before: int = 3, after: int = 3) -> pd.DataFrame:
    if df.empty:
        return df
    before_df = df[df["end"] < split_date].tail(before)
    after_df = df[df["end"] >= split_date].head(after)
    return pd.concat([before_df, after_df]).sort_values("end")


def implied_eps_table(eps_df: pd.DataFrame, ni_df: pd.DataFrame, shares_df: pd.DataFrame) -> pd.DataFrame:
    if eps_df.empty or ni_df.empty or shares_df.empty:
        return pd.DataFrame()

    base = eps_df[["end", "value"]].rename(columns={"value": "eps_reported"}).copy()

    def nearest_merge(left: pd.DataFrame, right: pd.DataFrame, value_name: str) -> pd.DataFrame:
        out = []
        for _, row in left.iterrows():
            date = row["end"]
            candidates = right.copy()
            candidates["dist"] = (candidates["end"] - date).abs().dt.days
            candidates = candidates[candidates["dist"] <= 45]
            if candidates.empty:
                value = None
                source_date = None
            else:
                chosen = candidates.sort_values("dist").iloc[0]
                value = chosen["value"]
                source_date = chosen["end"]
            out.append((value, source_date))
        left[value_name] = [x[0] for x in out]
        left[f"{value_name}_date"] = [x[1] for x in out]
        return left

    base = nearest_merge(base, ni_df, "net_income")
    base = nearest_merge(base, shares_df, "diluted_shares")
    base["eps_implied"] = base.apply(
        lambda r: r["net_income"] / r["diluted_shares"]
        if pd.notna(r["net_income"]) and pd.notna(r["diluted_shares"]) and r["diluted_shares"] != 0
        else float("nan"),
        axis=1,
    )
    base["eps_diff_pct"] = base.apply(
        lambda r: abs(r["eps_reported"] - r["eps_implied"]) / max(abs(r["eps_reported"]), 1e-9) * 100
        if pd.notna(r["eps_implied"]) else float("nan"),
        axis=1,
    )
    return base


def main() -> None:
    print("=" * 88)
    print("TEST SPLITS — yfinance + SEC/XBRL")
    print("=" * 88)

    for ticker in TICKERS:
        print("\n" + "=" * 88)
        print(f"TICKER: {ticker}")
        print("=" * 88)

        splits = get_yfinance_splits(ticker)
        print("\n[YFINANCE SPLITS]")
        if splits.empty:
            print("  Sin splits reportados")
        else:
            for date, ratio in splits.items():
                print(f"  {pd.Timestamp(date).date()} | ratio={float(ratio):g}")

        try:
            facts = get_sec_companyfacts(CIK_MAP[ticker])
        except Exception as exc:
            print(f"ERROR SEC: {type(exc).__name__}: {exc}")
            continue

        print("\n[SEC TAGS RELACIONADOS CON SPLIT]")
        tags = list_split_related_sec_tags(facts)
        if not tags:
            print("  Ninguno localizado")
        else:
            for tag, label in tags[:30]:
                print(f"  {tag} | {label}")

        eps = quarterly_fact_series(facts, "EarningsPerShareDiluted", ["USD/shares", "USD / shares"])
        ni = quarterly_fact_series(facts, "NetIncomeLoss", ["USD"])
        shares = quarterly_fact_series(facts, "WeightedAverageNumberOfDilutedSharesOutstanding", ["shares"])

        print("\n[COBERTURA SEC]")
        print(f"  EPS trimestral:       {len(eps)}")
        print(f"  Net income trimestral:{len(ni)}")
        print(f"  Diluted shares:       {len(shares)}")

        implied = implied_eps_table(eps, ni, shares)
        print("\n[EPS REPORTADO vs EPS IMPLÍCITO = NET INCOME / DILUTED SHARES]")
        if implied.empty:
            print("  No hay datos suficientes")
        else:
            for _, r in implied.tail(10).iterrows():
                imp = "N/D" if pd.isna(r["eps_implied"]) else f"{r['eps_implied']:.4f}"
                diff = "N/D" if pd.isna(r["eps_diff_pct"]) else f"{r['eps_diff_pct']:.2f}%"
                print(f"  {r['end'].date()} | reported={r['eps_reported']:.4f} | implied={imp} | diff={diff}")

        if not splits.empty and not eps.empty:
            recent_splits = splits[splits.index >= (pd.Timestamp.utcnow().tz_localize(None) - pd.DateOffset(years=6))]
            for split_date, ratio in recent_splits.items():
                split_date = pd.Timestamp(split_date)
                print(f"\n[VENTANA ALREDEDOR DEL SPLIT {split_date.date()} ratio={float(ratio):g}]")
                e = nearest_rows(eps, split_date)
                n = nearest_rows(ni, split_date)
                sh = nearest_rows(shares, split_date)

                print("  EPS:")
                for _, r in e.iterrows():
                    print(f"    {r['end'].date()} | {r['value']}")
                print("  Net income:")
                for _, r in n.iterrows():
                    print(f"    {r['end'].date()} | {r['value']}")
                print("  Diluted shares:")
                for _, r in sh.iterrows():
                    print(f"    {r['end'].date()} | {r['value']}")

    print("\n" + "=" * 88)
    print("FIN DEL TEST")
    print("=" * 88)


if __name__ == "__main__":
    main()
