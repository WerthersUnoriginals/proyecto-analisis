from __future__ import annotations

import pandas as pd

from fundamental_c import analyze_current_earnings

TICKERS = [
    "CVNA", "SMCI", "SNDK", "CDNS", "EXPD", "CVX", "FANG", "PSX", "XOM",
    "ZBRA", "FIS", "COP", "STT", "BRK-B", "MCO", "SNPS", "MRVL",
]
OUTPUT_CSV = "c_score_v12_low_persistence_cases.csv"


def main() -> None:
    rows = []
    failures = []
    for ticker in TICKERS:
        try:
            report = analyze_current_earnings(ticker)
            score = report.get("c_score_v1", {})
            rows.append({
                "ticker": ticker,
                "score": score.get("normalized_score"),
                "class": score.get("class"),
                "usability": score.get("usability"),
                "score_status": score.get("status"),
                "available_points": score.get("available_points"),
                "persistence": score.get("components", {}).get("persistence", {}).get("points"),
                "trend_quality": score.get("components", {}).get("eps_trend_quality", {}).get("points"),
                "eps_yoy": report.get("latest_eps_yoy_pct"),
                "eps_acc": report.get("eps_acceleration_pp"),
                "sales_yoy": report.get("latest_revenue_yoy_pct"),
                "sales_acc": report.get("revenue_acceleration_pp"),
                "flags": ",".join(report.get("c_flags", []) or []),
                "integrity": report.get("data_integrity"),
                "model_version": score.get("model_version"),
            })
        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})

    frame = pd.DataFrame(rows).sort_values(["score", "ticker"], ascending=[False, True], na_position="last")
    frame.to_csv(OUTPUT_CSV, index=False)
    print(frame.to_string(index=False))
    print("\nResumen:")
    print("éxito", len(frame), "/", len(TICKERS))
    print("fallos", failures)
    print("REVIEW", int((frame["usability"] == "C_SCORE_REVIEW").sum()))
    print("REVIEW_LOW_PERSISTENCE", int((frame["score_status"] == "REVIEW_LOW_PERSISTENCE").sum()))
    print("LOW_PERSISTENCE_HIGH_SCORE", int(frame["flags"].str.contains("LOW_PERSISTENCE_HIGH_SCORE", na=False).sum()))
    print("LOW_PERSISTENCE_BASE_EFFECT", int(frame["flags"].str.contains("LOW_PERSISTENCE_BASE_EFFECT", na=False).sum()))


if __name__ == "__main__":
    main()
