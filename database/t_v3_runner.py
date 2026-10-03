"""Evaluate the technical entry phase (Score Fase de Entrada v2) for one company at ``as_of``.

Usage::

    python -m database.t_v3_runner NVDA LRCX [--as-of 2026-10-03T00:00:00+00:00]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from database import evidence_v3
from database.annual_v3 import build_annual_view
from database.c_v3_runner import load_split_reconciliation
from database.entry_phase_v2 import compute_entry_phase
from database.prices_v3 import build_price_series
from database.t_contract_v1 import T_INPUT_KEYS, build_t_contract_v1


def evaluate_t(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None,
               capture_loader: Callable | None = None) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    sec_facts = evidence_v3.load_sec_facts(company_id, as_of, connection_factory=connection_factory)
    splits = load_split_reconciliation(company_id, as_of, sec_facts, capture_loader=capture_loader)
    view = build_annual_view(sec_facts, split_events=splits.events, as_of=as_of)
    applied = [event for event in splits.events if event not in view.rejected_split_events]
    series = build_price_series(evidence_v3.load_price_bars(company_id, as_of, connection_factory=connection_factory),
                                split_events=applied, as_of=as_of)
    contract = build_t_contract_v1(compute_entry_phase(series.bars, as_of.date()), series,
                                   company_id=company_id, as_of=as_of)
    return {"contract": contract}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the entry phase score (technical layer)")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    output = []
    for ticker in args.tickers:
        company = evidence_v3.load_company(ticker)
        if company is None:
            raise SystemExit(f"Ticker not found: {ticker}")
        contract = evaluate_t(company[0], as_of)["contract"]
        output.append({"ticker": ticker.upper(), **{key: contract[key] for key in T_INPUT_KEYS},
                       "entry_phase_ready": contract["entry_phase_ready"],
                       "diagnostics": contract["integrity"]["diagnostics"]})
    print(json.dumps(output, indent=2, default=str))
    return output


if __name__ == "__main__":
    main()
