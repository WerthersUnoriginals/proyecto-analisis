"""Stress test C Score v1.2 sobre Russell 1000 vía holdings de IWB.

Objetivos:
- Mantener intacto C Score v1.2.
- Usar una aproximación operativa reproducible al Russell 1000 mediante IWB.
- Filtrar posiciones no-equity y validar que el universo tenga tamaño plausible.
- Medir robustez de adquisición, integridad, usabilidad y anomalías.
- Guardar resultados y fallos para auditoría.
"""
from __future__ import annotations

from collections import Counter
from io import StringIO
import time

import pandas as pd
import requests

from fundamental_c import analyze_current_earnings

IWB_URL = "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
OUTPUT_CSV = "russell1000_c_score_v12_results.csv"
FAILURES_CSV = "russell1000_c_score_v12_failures.csv"
MIN_PLAUSIBLE_UNIVERSE = 900
MAX_PLAUSIBLE_UNIVERSE = 1150


def load_tickers() -> tuple[list[str], dict]:
    headers = {"User-Agent": "Mozilla/5.0 CANSLIMResearch/0.8"}
    response = requests.get(IWB_URL, headers=headers, timeout=60)
    response.raise_for_status()
    lines = response.text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
    frame = pd.read_csv(StringIO("\n".join(lines[start:])))

    raw_rows = len(frame)
    if "Asset Class" in frame.columns:
        equity_mask = frame["Asset Class"].astype(str).str.strip().str.lower().eq("equity")
        frame = frame[equity_mask].copy()
    equity_rows = len(frame)

    tickers: list[str] = []
    rejected: list[str] = []
    for value in frame.get("Ticker", pd.Series(dtype=str)).dropna():
        ticker = str(value).strip().upper().replace(".", "-")
        valid = (
            bool(ticker)
            and ticker not in {"-", "USD"}
            and ticker.replace("-", "").isalnum()
            and len(ticker) <= 10
        )
        if valid:
            tickers.append(ticker)
        else:
            rejected.append(ticker)

    unique = sorted(set(tickers))
    if not MIN_PLAUSIBLE_UNIVERSE <= len(unique) <= MAX_PLAUSIBLE_UNIVERSE:
        raise RuntimeError(
            f"Universo IWB no plausible: {len(unique)} tickers; esperado entre "
            f"{MIN_PLAUSIBLE_UNIVERSE} y {MAX_PLAUSIBLE_UNIVERSE}"
        )

    meta = {
        "raw_rows": raw_rows,
        "equity_rows": equity_rows,
        "unique_tickers": len(unique),
        "rejected_rows": len(rejected),
    }
    return unique, meta


def component(score: dict, name: str):
    return score.get("components", {}).get(name, {}).get("points")


def analyze_one(ticker: str) -> dict:
    report = analyze_current_earnings(ticker)
    score = report.get("c_score_v1", {})
    flags = report.get("c_flags", []) or []
    classic = report.get("c_classic", {})
    return {
        "ticker": ticker,
        "score": score.get("normalized_score"),
        "class": score.get("class"),
        "usability": score.get("usability"),
        "score_status": score.get("status"),
        "available_points": score.get("available_points"),
        "classic": classic.get("result"),
        "eps_yoy": report.get("latest_eps_yoy_pct"),
        "eps_acc": report.get("eps_acceleration_pp"),
        "sales_yoy": report.get("latest_revenue_yoy_pct"),
        "sales_acc": report.get("revenue_acceleration_pp"),
        "persistence": component(score, "persistence"),
        "trend_quality": component(score, "eps_trend_quality"),
        "integrity": report.get("data_integrity"),
        "data_quality": report.get("data_quality"),
        "shares_quality": report.get("shares_quality"),
        "cik_source": report.get("cik_source"),
        "eps_yoy_calculable": report.get("eps_yoy_calculable"),
        "revenue_yoy_calculable": report.get("revenue_yoy_calculable"),
        "flags": ",".join(flags) if flags else "-",
    }


def main() -> None:
    tickers, universe_meta = load_tickers()
    print("=" * 140)
    print("RUSSELL 1000 STRESS TEST — C SCORE V1.2")
    print("=" * 140)
    print("Fuente operativa: holdings IWB (iShares Russell 1000 ETF)")
    print("Universo:", universe_meta)

    rows: list[dict] = []
    failures: list[dict] = []
    started = time.time()

    for i, ticker in enumerate(tickers, 1):
        try:
            rows.append(analyze_one(ticker))
        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})
        if i == 1 or i % 25 == 0 or i == len(tickers):
            print(
                f"Progreso {i}/{len(tickers)} | exito={len(rows)} | "
                f"fallos={len(failures)} | min={(time.time()-started)/60:.1f}"
            )
        time.sleep(0.10)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["score", "ticker"], ascending=[False, True], na_position="last")
        frame.to_csv(OUTPUT_CSV, index=False)
    pd.DataFrame(failures, columns=["ticker", "error"]).to_csv(FAILURES_CSV, index=False)

    valid = frame["score"].dropna()
    high = frame[frame["score"] >= 70]
    review = frame[frame["usability"] == "C_SCORE_REVIEW"]
    lowp = high[high["persistence"].fillna(99) <= 2]
    partial = frame[(frame["available_points"].fillna(0) < 80) | (frame["score_status"] == "PARTIAL_SCORE")]
    base = frame[frame["flags"].str.contains("BASE_EFFECT_RISK|SMALL_BASE_RISK", regex=True, na=False)]
    extreme_eps = frame[frame["eps_yoy"].abs() >= 200]

    print("\nRESUMEN")
    print("universo", len(tickers), "exito", len(frame), "fallos", len(failures), "scores_validos", len(valid))
    print("tasa_exito_pct", round(len(frame) / len(tickers) * 100, 2) if tickers else 0)
    print("duracion_min", round((time.time() - started) / 60, 2))
    if len(valid):
        print(
            "media", round(valid.mean(), 2), "mediana", round(valid.median(), 2),
            "p10", round(valid.quantile(.10), 2), "p25", round(valid.quantile(.25), 2),
            "p75", round(valid.quantile(.75), 2), "p90", round(valid.quantile(.90), 2)
        )
    print("clases", frame["class"].value_counts(dropna=False).to_dict())
    print("usabilidad", frame["usability"].value_counts(dropna=False).to_dict())
    print("score_status", frame["score_status"].value_counts(dropna=False).to_dict())
    print("integridad", frame["integrity"].value_counts(dropna=False).to_dict())
    print("available_points", frame["available_points"].value_counts(dropna=False).sort_index().to_dict())
    print("score>=70", len(high), ">=80", int((frame["score"] >= 80).sum()), ">=90", int((frame["score"] >= 90).sum()))
    print(">=70 usable", len(high[high["usability"] == "C_SCORE_USABLE"]), ">=70 review", len(high[high["usability"] == "C_SCORE_REVIEW"]))
    print(">=70 persistencia<=2", len(lowp), "review_total", len(review))
    print("partial_o_available<80", len(partial), "base_or_small_base", len(base), "eps_extremo", len(extreme_eps))

    counter = Counter()
    for text in frame["flags"].fillna("-"):
        if text != "-":
            for flag in str(text).split(","):
                counter[flag] += 1
    print("flags", dict(counter.most_common()))

    cols = ["ticker", "score", "class", "usability", "persistence", "trend_quality", "eps_yoy", "sales_yoy", "flags", "integrity"]
    print("\nTOP30")
    print(frame[cols].head(30).to_string(index=False))
    print("\nLOW_PERSISTENCE_HIGH_SCORE")
    print(lowp[cols].to_string(index=False))
    print("\nTOP_REVIEW")
    print(review[cols].head(50).to_string(index=False))
    if failures:
        print("\nFAILURES")
        for item in failures:
            print(item["ticker"], item["error"])
    print("\nCSV", OUTPUT_CSV)
    print("FAILURES_CSV", FAILURES_CSV)


if __name__ == "__main__":
    main()
