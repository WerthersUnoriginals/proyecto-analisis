"""Batch ingestion and C/A/N evaluation of many companies.

Usage::

    python -m database.batch_v3 AAPL MSFT NVDA [--ingest] [--file tickers.txt] [--out report.json]

One company failing never stops the batch; its error is reported in its row.
Without ``--as-of``, the evaluation time is taken after ingestion: an ``as_of``
fixed before ingesting would, correctly, hide everything just observed.
"""

from __future__ import annotations

import argparse
import json
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


def _default_evaluate(ticker: str, as_of: datetime):
    from database import evidence_v3
    from database.a_v3_runner import evaluate_a
    from database.c_v3_runner import evaluate_c_v3
    from database.n_v3_runner import evaluate_n

    company = evidence_v3.load_company(ticker)
    if company is None:
        raise LookupError(f"ticker not ingested: {ticker}")
    return evaluate_c_v3(company[0], as_of), evaluate_a(company[0], as_of), evaluate_n(company[0], as_of)


def _default_ingest(ticker: str):
    from database.ingest_v3 import ingest_company

    return ingest_company(ticker)


def _n_fields(n_result: dict | None) -> dict:
    if n_result is None:
        return {}
    contract = n_result["contract"]
    score = n_result.get("score")
    return {
        "n_score": score["n_score_v1"]["normalized_score"] if score else None,
        "n_usability": score["n_score_v1"]["usability"] if score else None,
        "n_classic": score["n_classic"]["result"] if score else None,
        "n_status": contract["n_status"],
        "n_data_integrity": contract["price_data_integrity"],
        "n_pct_below_high_52w": contract["pct_below_high_52w"],
        "n_new_high_recent": contract["new_high_recent"],
        "n_catalysts": contract["catalysts"]["counts"],
        "n_diagnostics": contract["integrity"]["diagnostics"],
    }


def _row(ticker: str, c_result: dict, a_result: dict, n_result: dict | None = None) -> dict:
    c_contract, c_score = c_result["contract"], c_result["score"]
    a_contract, a_score = a_result["contract"], a_result["score"]
    return {
        "ticker": ticker,
        "error": None,
        "c_score": c_score["c_score_v1"]["normalized_score"],
        "c_class": c_score["c_score_v1"]["class"],
        "c_usability": c_score["c_score_v1"]["usability"],
        "c_data_integrity": c_contract["data_integrity"],
        "c_latest_eps_yoy_pct": c_contract["latest_eps_yoy_pct"],
        "c_diagnostics": c_contract["integrity"]["diagnostics"],
        "a_score": a_score["a_score_v1"]["normalized_score"],
        "a_class": a_score["a_score_v1"]["class"],
        "a_usability": a_score["a_score_v1"]["usability"],
        "a_classic": a_score["a_classic"]["result"],
        "a_data_integrity": a_contract["annual_data_integrity"],
        "a_diagnostics": a_contract["integrity"]["diagnostics"],
        **_n_fields(n_result),
    }


def _error_row(ticker: str, error: Exception) -> dict:
    return {"ticker": ticker, "error": type(error).__name__,
            "error_detail": traceback.format_exception_only(error)[-1].strip()[:300]}


def run_batch(
    tickers: Iterable[str],
    *,
    as_of: datetime | None,
    ingest: Callable | None = None,
    evaluate: Callable = _default_evaluate,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[dict]:
    """Ingest every ticker first, then evaluate all of them at one ``as_of``.

    ``as_of=None`` evaluates at the clock time after ingestion.
    """

    tickers = [ticker.strip().upper() for ticker in tickers]
    failed: dict[str, dict] = {}
    if ingest is not None:
        for ticker in tickers:
            try:
                ingest(ticker)
            except Exception as error:  # one company never stops the batch
                failed[ticker] = _error_row(ticker, error)
    as_of = as_of or clock()
    rows = []
    for ticker in tickers:
        if ticker in failed:
            rows.append(failed[ticker])
            continue
        try:
            rows.append(_row(ticker, *evaluate(ticker, as_of)))
        except Exception as error:  # one company never stops the batch
            rows.append(_error_row(ticker, error))
    return rows


def summarize(rows: list[dict]) -> dict:
    ok = [row for row in rows if not row.get("error")]
    return {
        "companies": len(rows),
        "errors": len(rows) - len(ok),
        "c_data_integrity": Counter(row["c_data_integrity"] for row in ok),
        "c_usability": Counter(row["c_usability"] for row in ok),
        "a_data_integrity": Counter(row["a_data_integrity"] for row in ok),
        "a_classic": Counter(row["a_classic"] for row in ok),
        "n_data_integrity": Counter(row.get("n_data_integrity") for row in ok if "n_data_integrity" in row),
        "n_classic": Counter(row.get("n_classic") for row in ok if "n_classic" in row),
        "diagnostics": Counter(
            item.split(":")[0] for row in ok
            for item in row["c_diagnostics"] + row["a_diagnostics"] + row.get("n_diagnostics", [])
        ),
    }


def _pct(value) -> str:
    return "-" if value is None else f"{value:.1f}%"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Batch C/A/N evaluation")
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--file", help="text file with one ticker per line")
    parser.add_argument("--ingest", action="store_true", help="ingest each ticker before evaluating")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--out", help="write rows and summary as JSON")
    args = parser.parse_args(argv)
    tickers = list(args.tickers)
    if args.file:
        tickers += [line.strip() for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
    fixed_as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    evaluated_at = []

    def evaluate(ticker, as_of):
        evaluated_at.append(as_of)
        return _default_evaluate(ticker, as_of)

    rows = run_batch(tickers, as_of=fixed_as_of, ingest=_default_ingest if args.ingest else None, evaluate=evaluate)
    as_of = evaluated_at[0] if evaluated_at else fixed_as_of or datetime.now(timezone.utc)
    summary = summarize(rows)
    for row in rows:
        if row.get("error"):
            print(f"{row['ticker']:7} ERROR {row['error_detail']}")
        else:
            print(f"{row['ticker']:7} C {row['c_score']!s:>6} {row['c_usability']:15} {row['c_data_integrity']:40} "
                  f"A {row['a_score']!s:>6} {row['a_classic']:22} {row['a_data_integrity']:32} "
                  f"N {row.get('n_score')!s:>6} {_pct(row.get('n_pct_below_high_52w')):>6} "
                  f"{row.get('n_classic') or '':18} {row.get('n_data_integrity', '')}")
    print(json.dumps(summary, indent=2, default=dict))
    if args.out:
        Path(args.out).write_text(json.dumps({"as_of": as_of.isoformat(), "rows": rows, "summary": summary},
                                             indent=2, default=dict), encoding="utf-8")
    return rows, summary


if __name__ == "__main__":
    main()
