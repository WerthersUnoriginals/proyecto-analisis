"""
Stress test del C Score v1.1 experimental sobre 20 compañías aleatorias del S&P 500.

- Descarga la composición actual del S&P 500 desde Wikipedia.
- Usa semilla fija para que la muestra sea reproducible.
- Ejecuta analyze_current_earnings() sin modificar los pesos del score.
- Resume ranking, flags, C_CLASSIC, usabilidad e integridad de datos.
"""

from __future__ import annotations

from io import StringIO
import random
import time

import pandas as pd
import requests

from fundamental_c import analyze_current_earnings


SAMPLE_SIZE = 20
RANDOM_SEED = 260901
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def load_sp500_tickers() -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 CANSLIMResearch/0.6"}
    response = requests.get(SP500_URL, headers=headers, timeout=30)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    if not tables:
        raise RuntimeError("No se pudo leer la tabla del S&P 500")
    table = tables[0]
    if "Symbol" not in table.columns:
        raise RuntimeError("La tabla del S&P 500 no contiene la columna Symbol")
    tickers = [str(x).strip().upper().replace(".", "-") for x in table["Symbol"].dropna()]
    return sorted(set(tickers))


def select_sample(tickers: list[str]) -> list[str]:
    rng = random.Random(RANDOM_SEED)
    if len(tickers) < SAMPLE_SIZE:
        raise RuntimeError(f"Universo insuficiente: {len(tickers)}")
    return sorted(rng.sample(tickers, SAMPLE_SIZE))


def fmt(value, digits=2):
    if value is None:
        return "N/D"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def main() -> None:
    universe = load_sp500_tickers()
    sample = select_sample(universe)

    print("=" * 125)
    print("STRESS TEST C SCORE V1.1 — 20 ACCIONES ALEATORIAS DEL S&P 500")
    print("=" * 125)
    print(f"Universo leído: {len(universe)} tickers")
    print(f"Semilla: {RANDOM_SEED}")
    print("Muestra:", ", ".join(sample))
    print()

    rows = []
    failures = []

    for i, ticker in enumerate(sample, start=1):
        print(f"[{i:02d}/{len(sample)}] Analizando {ticker}...")
        try:
            report = analyze_current_earnings(ticker)
            score = report.get("c_score_v1", {})
            classic = report.get("c_classic", {})
            small_base = score.get("small_base", {})
            rows.append({
                "ticker": ticker,
                "score": score.get("normalized_score"),
                "class": score.get("class"),
                "score_status": score.get("status"),
                "usability": score.get("usability"),
                "classic": classic.get("result"),
                "eps_yoy": report.get("latest_eps_yoy_pct"),
                "eps_acc": report.get("eps_acceleration_pp"),
                "sales_yoy": report.get("latest_revenue_yoy_pct"),
                "sales_acc": report.get("revenue_acceleration_pp"),
                "persistence": score.get("components", {}).get("persistence", {}).get("points"),
                "trend_quality": score.get("components", {}).get("eps_trend_quality", {}).get("points"),
                "prior_eps_inferred": small_base.get("inferred_previous_eps"),
                "flags": ",".join(report.get("c_flags", [])) or "-",
                "integrity": report.get("data_integrity"),
                "available_points": score.get("available_points"),
            })
        except Exception as exc:
            failures.append((ticker, repr(exc)))
            print(f"  ERROR {ticker}: {exc!r}")
        time.sleep(0.25)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["score", "ticker"], ascending=[False, True], na_position="last")
        print("\n" + "=" * 125)
        print("RANKING")
        print("=" * 125)
        columns = [
            "ticker", "score", "class", "usability", "classic", "eps_yoy", "eps_acc",
            "sales_yoy", "persistence", "trend_quality", "flags", "integrity"
        ]
        print(frame[columns].to_string(index=False))

        print("\n" + "=" * 125)
        print("RESUMEN")
        print("=" * 125)
        valid_scores = frame["score"].dropna()
        print(f"Analizadas con éxito: {len(frame)}/{len(sample)}")
        print(f"Fallos: {len(failures)}")
        print(f"Score medio: {fmt(valid_scores.mean() if len(valid_scores) else None)}")
        print(f"Mediana: {fmt(valid_scores.median() if len(valid_scores) else None)}")
        print(f">=80: {int((valid_scores >= 80).sum())}")
        print(f">=70: {int((valid_scores >= 70).sum())}")
        print(f"<50: {int((valid_scores < 50).sum())}")
        print("Clases:", frame["class"].value_counts(dropna=False).to_dict())
        print("C Classic:", frame["classic"].value_counts(dropna=False).to_dict())
        print("Usabilidad:", frame["usability"].value_counts(dropna=False).to_dict())
        print("Integridad:", frame["integrity"].value_counts(dropna=False).to_dict())

        base_effect = frame[frame["flags"].str.contains("BASE_EFFECT_RISK", na=False)]
        print(f"BASE_EFFECT_RISK: {len(base_effect)}")
        if not base_effect.empty:
            print(base_effect[["ticker", "score", "eps_yoy", "eps_acc", "persistence", "trend_quality", "flags"]].to_string(index=False))

        small_base = frame[frame["flags"].str.contains("SMALL_BASE_RISK", na=False)]
        print(f"SMALL_BASE_RISK: {len(small_base)}")
        if not small_base.empty:
            print(small_base[["ticker", "score", "eps_yoy", "prior_eps_inferred", "persistence", "trend_quality", "flags"]].to_string(index=False))

        review = frame[frame["usability"] == "C_SCORE_REVIEW"]
        print(f"C_SCORE_REVIEW: {len(review)}")
        if not review.empty:
            print(review[["ticker", "score", "score_status", "available_points", "integrity", "flags"]].to_string(index=False))

        high_score_low_persistence = frame[(frame["score"] >= 70) & (frame["persistence"].fillna(0) <= 3)]
        print(f"Score >=70 con persistencia <=3: {len(high_score_low_persistence)}")
        if not high_score_low_persistence.empty:
            print(high_score_low_persistence[["ticker", "score", "eps_yoy", "eps_acc", "sales_yoy", "persistence", "trend_quality", "flags"]].to_string(index=False))

        high_score_review = frame[(frame["score"] >= 60) & (frame["usability"] == "C_SCORE_REVIEW")]
        print(f"Score >=60 marcado REVIEW: {len(high_score_review)}")
        if not high_score_review.empty:
            print(high_score_review[["ticker", "score", "class", "score_status", "integrity", "flags"]].to_string(index=False))

    if failures:
        print("\nFALLOS")
        for ticker, error in failures:
            print(f"  {ticker}: {error}")


if __name__ == "__main__":
    main()
