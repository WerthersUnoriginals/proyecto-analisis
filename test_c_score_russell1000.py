"""Stress test C Score v1.2 sobre snapshot público del Russell 1000.

Objetivos:
- Mantener intacto C Score v1.2.
- Usar un universo fijo y reproducible de ~1000 compañías reales.
- Medir robustez de adquisición, integridad, usabilidad y anomalías.
- Guardar resultados y fallos para auditoría.

El snapshot de componentes de Wikipedia está fechado 2026-04-02. Para este
stress test importa la heterogeneidad y escala del universo, no replicar un
rebalanceo actual del índice.
"""
from __future__ import annotations

from collections import Counter
from io import StringIO
import time

import pandas as pd
import requests

from fundamental_c import analyze_current_earnings

R1000_URL = "https://en.wikipedia.org/wiki/Russell_1000_Index"
R1000_SNAPSHOT_DATE = "2026-04-02"
OUTPUT_CSV = "russell1000_c_score_v12_results.csv"
FAILURES_CSV = "russell1000_c_score_v12_failures.csv"
MIN_PLAUSIBLE_UNIVERSE = 900
MAX_PLAUSIBLE_UNIVERSE = 1150


def load_tickers() -> tuple[list[str], dict]:
    headers = {"User-Agent": "Mozilla/5.0 CANSLIMResearch/1.1"}
    response = requests.get(R1000_URL, headers=headers, timeout=60)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))

    candidates = []
    for table in tables:
        cols = {str(c).strip() for c in table.columns}
        if "Symbol" in cols and len(table) >= MIN_PLAUSIBLE_UNIVERSE:
            candidates.append(table)
    if not candidates:
        raise RuntimeError(
            f"No se encontró tabla plausible Russell 1000: tablas={len(tables)}, "
            f"filas={[len(t) for t in tables[:20]]}"
        )

    frame = max(candidates, key=len)
    raw_rows = len(frame)
    tickers = []
    rejected = []
    for value in frame["Symbol"].dropna():
        ticker = str(value).strip().upper().replace(".", "-")
        valid = (
            bool(ticker)
            and ticker not in {"-", "USD", "NONE", "NAN"}
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
            f"Universo Russell 1000 no plausible: {len(unique)} tickers; "
            f"esperado entre {MIN_PLAUSIBLE_UNIVERSE} y {MAX_PLAUSIBLE_UNIVERSE}"
        )

    meta = {
        "snapshot_date": R1000_SNAPSHOT_DATE,
        "raw_rows": raw_rows,
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
    print("Fuente operativa: snapshot público Russell 1000 (Wikipedia)")
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
