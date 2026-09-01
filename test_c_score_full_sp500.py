"""
Validación integral del módulo C (fundamental_c + C Score v1.1) sobre todo el S&P 500.

Objetivos:
- Analizar todos los constituyentes actuales del S&P 500 (pueden ser >500 tickers por clases de acciones).
- No modificar las reglas del score: esta prueba es puramente diagnóstica.
- Guardar un CSV completo para auditoría.
- Imprimir un resumen compacto con distribución, integridad, flags y anomalías.
"""

from __future__ import annotations

from collections import Counter
from io import StringIO
import time

import pandas as pd
import requests

from fundamental_c import analyze_current_earnings


SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
OUTPUT_CSV = "sp500_c_score_full_results.csv"


def load_sp500_tickers() -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 CANSLIMResearch/0.6"}
    response = requests.get(SP500_URL, headers=headers, timeout=30)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    if not tables or "Symbol" not in tables[0].columns:
        raise RuntimeError("No se pudo obtener la tabla de constituyentes del S&P 500")
    tickers = [
        str(symbol).strip().upper().replace(".", "-")
        for symbol in tables[0]["Symbol"].dropna()
    ]
    return sorted(set(tickers))


def _component(score: dict, name: str, key: str = "points"):
    return score.get("components", {}).get(name, {}).get(key)


def analyze_one(ticker: str) -> dict:
    report = analyze_current_earnings(ticker)
    score = report.get("c_score_v1", {})
    classic = report.get("c_classic", {})
    flags = report.get("c_flags", []) or []
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
        "eps_growth_points": _component(score, "eps_growth"),
        "eps_acc_points": _component(score, "eps_acceleration"),
        "trend_quality": _component(score, "eps_trend_quality"),
        "sales_growth_points": _component(score, "sales_growth"),
        "sales_acc_points": _component(score, "sales_acceleration"),
        "persistence": _component(score, "persistence"),
        "integrity": report.get("data_integrity"),
        "flags": ",".join(flags) if flags else "-",
        "cik_source": report.get("cik_source"),
        "eps_source": report.get("eps_source"),
        "revenue_source": report.get("revenue_source"),
        "shares_quality": report.get("shares_quality"),
    }


def _print_table(title: str, frame: pd.DataFrame, columns: list[str], n: int = 20) -> None:
    print("\n" + "=" * 140)
    print(title)
    print("=" * 140)
    if frame.empty:
        print("Ningún caso")
        return
    print(frame[columns].head(n).to_string(index=False))


def main() -> None:
    tickers = load_sp500_tickers()
    print("=" * 140)
    print("VALIDACIÓN INTEGRAL C SCORE V1.1 — S&P 500 COMPLETO")
    print("=" * 140)
    print(f"Constituyentes/tickers únicos detectados: {len(tickers)}")
    print("Nota: el índice puede contener más de 500 tickers por compañías con varias clases de acciones.")

    rows: list[dict] = []
    failures: list[dict] = []
    started = time.time()

    for i, ticker in enumerate(tickers, start=1):
        try:
            rows.append(analyze_one(ticker))
        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})
        if i == 1 or i % 25 == 0 or i == len(tickers):
            elapsed = time.time() - started
            print(f"Progreso {i}/{len(tickers)} | éxito={len(rows)} | fallos={len(failures)} | {elapsed/60:.1f} min")
        time.sleep(0.10)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["score", "ticker"], ascending=[False, True], na_position="last")
        frame.to_csv(OUTPUT_CSV, index=False)

    elapsed = time.time() - started
    print("\n" + "=" * 140)
    print("RESUMEN GENERAL")
    print("=" * 140)
    print(f"Universo: {len(tickers)}")
    print(f"Analizadas con éxito: {len(frame)}")
    print(f"Fallos: {len(failures)}")
    print(f"Tasa de éxito: {(len(frame)/len(tickers)*100 if tickers else 0):.2f}%")
    print(f"Duración: {elapsed/60:.2f} min")

    if frame.empty:
        if failures:
            print("Fallos:", failures)
        raise SystemExit(1)

    valid = frame["score"].dropna()
    print(f"Scores válidos: {len(valid)}/{len(frame)}")
    print(f"Score medio: {valid.mean():.2f}" if len(valid) else "Score medio: N/D")
    print(f"Mediana: {valid.median():.2f}" if len(valid) else "Mediana: N/D")
    print(f"P10/P25/P75/P90: {valid.quantile(.10):.2f} / {valid.quantile(.25):.2f} / {valid.quantile(.75):.2f} / {valid.quantile(.90):.2f}" if len(valid) else "Percentiles: N/D")
    print("Clases:", frame["class"].value_counts(dropna=False).to_dict())
    print("Usabilidad:", frame["usability"].value_counts(dropna=False).to_dict())
    print("Score status:", frame["score_status"].value_counts(dropna=False).to_dict())
    print("C Classic:", frame["classic"].value_counts(dropna=False).to_dict())
    print("Integridad:", frame["integrity"].value_counts(dropna=False).to_dict())
    print("Available points:", frame["available_points"].value_counts(dropna=False).sort_index().to_dict())

    bins = [-0.01, 20, 40, 50, 60, 70, 80, 90, 100.01]
    labels = ["0-19.99", "20-39.99", "40-49.99", "50-59.99", "60-69.99", "70-79.99", "80-89.99", "90-100"]
    score_bins = pd.cut(frame["score"], bins=bins, labels=labels, include_lowest=True, right=False)
    print("Distribución por tramos:", score_bins.value_counts(sort=False, dropna=False).to_dict())

    flag_counter = Counter()
    for text in frame["flags"].fillna("-"):
        if text != "-":
            for flag in str(text).split(","):
                flag_counter[flag] += 1
    print("Flags:", dict(flag_counter.most_common()))

    cols = ["ticker", "score", "class", "usability", "classic", "eps_yoy", "eps_acc", "sales_yoy", "sales_acc", "persistence", "trend_quality", "flags", "integrity"]
    _print_table("TOP 30 C SCORE", frame, cols, 30)
    _print_table("BOTTOM 20 C SCORE", frame.sort_values(["score", "ticker"], ascending=[True, True], na_position="last"), cols, 20)

    review = frame[frame["usability"] == "C_SCORE_REVIEW"].sort_values("score", ascending=False, na_position="last")
    _print_table("C_SCORE_REVIEW — TOP 30 POR SCORE", review, ["ticker", "score", "class", "score_status", "available_points", "flags", "integrity"], 30)

    high_low_persistence = frame[(frame["score"] >= 70) & (frame["persistence"].fillna(0) <= 3)].sort_values("score", ascending=False)
    _print_table("ANOMALÍA: SCORE >=70 CON PERSISTENCIA <=3", high_low_persistence, cols, 50)

    high_review = frame[(frame["score"] >= 70) & (frame["usability"] == "C_SCORE_REVIEW")].sort_values("score", ascending=False)
    _print_table("SCORE >=70 PERO REVIEW", high_review, ["ticker", "score", "class", "score_status", "available_points", "flags", "integrity"], 50)

    extreme_eps = frame[frame["eps_yoy"].abs() >= 200].sort_values("eps_yoy", ascending=False)
    _print_table("EPS YOY EXTREMO (ABS >=200%)", extreme_eps, ["ticker", "score", "eps_yoy", "eps_acc", "persistence", "trend_quality", "flags", "integrity"], 50)

    base_or_small = frame[frame["flags"].str.contains("BASE_EFFECT_RISK|SMALL_BASE_RISK", regex=True, na=False)].sort_values("score", ascending=False)
    _print_table("CASOS BASE_EFFECT_RISK / SMALL_BASE_RISK", base_or_small, ["ticker", "score", "eps_yoy", "eps_acc", "sales_yoy", "persistence", "trend_quality", "flags", "integrity"], 100)

    partial = frame[(frame["available_points"].fillna(0) < 80) | (frame["score_status"] == "PARTIAL_SCORE")].sort_values("score", ascending=False, na_position="last")
    _print_table("DATOS INSUFICIENTES / PARTIAL SCORE", partial, ["ticker", "score", "score_status", "available_points", "flags", "integrity"], 100)

    usable_high = frame[(frame["score"] >= 70) & (frame["usability"] == "C_SCORE_USABLE")]
    print("\n" + "=" * 140)
    print("CHEQUEOS DE SANIDAD")
    print("=" * 140)
    print(f"Score >=70: {(frame['score'] >= 70).sum()}")
    print(f"Score >=80: {(frame['score'] >= 80).sum()}")
    print(f"Score >=90: {(frame['score'] >= 90).sum()}")
    print(f"Score >=70 y USABLE: {len(usable_high)}")
    print(f"Score >=70 y REVIEW: {len(high_review)}")
    print(f"Score >=70 con persistencia <=3: {len(high_low_persistence)}")
    print(f"REVIEW total: {len(review)}")
    print(f"Partial/available<80: {len(partial)}")
    print(f"Base/small-base flags: {len(base_or_small)}")

    if failures:
        print("\n" + "=" * 140)
        print("FALLOS")
        print("=" * 140)
        for item in failures:
            print(f"{item['ticker']}: {item['error']}")

    print(f"\nCSV completo generado: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
