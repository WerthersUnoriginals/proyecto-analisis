"""Experience Store commands (spec 2026-10-04-experience-store-v1 §4).

Usage::

    python -m database.experience_v1 snapshot AAPL NVDA ... [--file tickers.txt] [--label weekly]
    python -m database.experience_v1 report [--as-of ISO] [--out report.json]

A snapshot evaluates every letter from stored evidence and records it
immutably; a report derives forward outcomes from the prices visible at report
time.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from database.experience_outcomes_v1 import OUTCOMES_VERSION, calibration, compute_outcome

LETTER_CODES = ("C", "A", "N", "S", "L", "I")
BENCHMARK = "SPY"


def _plain(value):
    """JSON-safe copy (Decimals, dates and tuples become strings and lists)."""

    return json.loads(json.dumps(value, default=str))


def _code_commit() -> str | None:
    """git HEAD, suffixed ``-dirty`` when tracked files have uncommitted changes."""

    root = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                              cwd=root).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                               text=True, timeout=10, cwd=root).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return (head + "-dirty" if dirty else head) or None


def build_record(ticker: str, results: tuple) -> dict:
    """One experience record from the letter results (C, A, N, S, L, I, M) and the technical layer (T)."""

    from canslim_score_v1 import build_canslim_score

    letters = dict(zip(LETTER_CODES, results[:6]))
    market = results[6]
    composite = build_canslim_score(letters, market)
    n_contract = letters["N"]["contract"]
    return {
        "ticker": ticker,
        "composite_score": composite["composite_score"],
        "verdict": composite["verdict"],
        "letters_passed": composite["letters_passed"],
        "data_status": composite["data_status"],
        "entry_bar_date": n_contract.get("last_bar_date"),
        "entry_close": n_contract.get("close_last"),
        "payload": _plain({"letters": letters, "composite": composite,
                           "technical": results[7] if len(results) > 7 else None}),
        "market": _plain(market),
    }


def take_snapshot(tickers: Iterable[str], *, label: str | None = None, clock: Callable | None = None,
                  evaluate: Callable | None = None, connection_factory: Callable | None = None) -> dict:
    from psycopg.types.json import Jsonb

    from database import evidence_v3
    from database.batch_v3 import _default_evaluate
    from database.ingest_v3 import _connect

    as_of = (clock or (lambda: datetime.now(timezone.utc)))()
    evaluate = evaluate or _default_evaluate
    records, errors = [], []
    for ticker in dict.fromkeys(item.strip().upper() for item in tickers):
        try:
            company = evidence_v3.load_company(ticker, connection_factory=connection_factory)
            if company is None:
                raise LookupError(f"ticker not ingested: {ticker}")
            records.append((company[0], build_record(ticker, evaluate(ticker, as_of))))
        except Exception as error:  # one company never stops the snapshot
            errors.append({"ticker": ticker, "error": type(error).__name__, "detail": str(error)[:200]})
    if not records:
        return {"snapshot_id": None, "as_of": as_of.isoformat(), "records": 0, "errors": errors}
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO public.experience_snapshots (as_of, label, code_commit, market)
               VALUES (%s, %s, %s, %s) RETURNING id;""",
            (as_of, label, _code_commit(), Jsonb(records[0][1]["market"])),
        )
        snapshot_id = cursor.fetchone()[0]
        cursor.executemany(
            """INSERT INTO public.experience_records (snapshot_id, company_id, ticker, composite_score, verdict,
                   letters_passed, data_status, entry_bar_date, entry_close, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
            [(snapshot_id, company_id, record["ticker"], record["composite_score"], record["verdict"],
              record["letters_passed"], record["data_status"], record["entry_bar_date"], record["entry_close"],
              Jsonb(record["payload"])) for company_id, record in records],
        )
    return {"snapshot_id": snapshot_id, "as_of": as_of.isoformat(), "records": len(records), "errors": errors}


LOAD_RECORDS_SQL = """
    SELECT s.id, s.as_of, s.label, r.company_id, r.ticker, r.composite_score, r.verdict, r.letters_passed,
           r.data_status, r.entry_bar_date
    FROM public.experience_records AS r JOIN public.experience_snapshots AS s ON s.id = r.snapshot_id
    WHERE s.created_at <= %s
    ORDER BY s.id, r.ticker;
"""


def build_report(as_of: datetime, *, connection_factory: Callable | None = None) -> dict:
    from database import evidence_v3
    from database.ingest_v3 import _connect
    from database.prices_v3 import build_price_series

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_RECORDS_SQL, (as_of,))
        columns = [item.name for item in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    ids = evidence_v3.load_company_ids([BENCHMARK], connection_factory=connection_factory)
    company_ids = {row["company_id"] for row in rows} | set(ids.values())
    bars = evidence_v3.load_many_price_bars(list(company_ids), as_of, connection_factory=connection_factory)
    series = {company_id: build_price_series(items, split_events=(), as_of=as_of)
              for company_id, items in bars.items()}
    market = series[ids[BENCHMARK]].bars if BENCHMARK in ids else ()
    records = []
    for row in rows:
        stock = series.get(row["company_id"])
        if stock is None or row["entry_bar_date"] is None:
            outcome = {"status": "NO_ENTRY_PRICE"}
        elif stock.review_reasons:
            outcome = {"status": "PRICE_REVIEW", "reasons": list(stock.review_reasons)}
        else:
            outcome = compute_outcome(stock.bars, market, row["entry_bar_date"])
        records.append({**row, "composite_score": None if row["composite_score"] is None
                        else float(row["composite_score"]), "outcome": outcome})
    return {"as_of": as_of.isoformat(), "version": OUTCOMES_VERSION, "records": records,
            "calibration": calibration(records)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experience Store: snapshots and forward outcomes")
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot")
    snap.add_argument("tickers", nargs="*")
    snap.add_argument("--file")
    snap.add_argument("--label")
    report = commands.add_parser("report")
    report.add_argument("--as-of", default=None)
    report.add_argument("--out")
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        tickers = list(args.tickers)
        if args.file:
            tickers += [line.strip() for line in Path(args.file).read_text(encoding="utf-8").splitlines()
                        if line.strip()]
        result = take_snapshot(tickers, label=args.label)
        print(json.dumps(result, indent=2, default=str))
        return result
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    result = build_report(as_of)
    matured = sum(1 for record in result["records"]
                  for item in (record["outcome"].get("horizons") or {}).values() if item.get("status") == "MATURED")
    print(f"records {len(result['records'])}, matured horizon outcomes {matured}")
    print(json.dumps(result["calibration"], indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


if __name__ == "__main__":
    main()
