"""Evaluate the A contract for one company at ``as_of`` from persisted evidence.

Usage::

    python -m database.a_v3_runner AAPL NVDA [--as-of 2026-10-02T00:00:00+00:00]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from a_score_v1 import build_a_score
from database import evidence_v3
from database.a_contract_v1 import A_INPUT_KEYS, build_a_contract_v1
from database.annual_v3 import build_annual_view
from database.c_v3_runner import load_split_reconciliation
from database.registrant_v3 import classify_filer


def evaluate_a(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None,
               capture_loader: Callable | None = None) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    sec_facts = evidence_v3.load_sec_facts(company_id, as_of, connection_factory=connection_factory)
    splits = load_split_reconciliation(company_id, as_of, sec_facts, capture_loader=capture_loader)
    view = build_annual_view(sec_facts, split_events=splits.events, as_of=as_of)
    filer_status = classify_filer(
        evidence_v3.load_filing_forms(company_id, as_of, connection_factory=connection_factory), as_of,
    )
    links = tuple(evidence_v3.load_registrant_links(company_id, as_of, connection_factory=connection_factory))
    contract = build_a_contract_v1(
        view, splits, company_id=company_id, as_of=as_of, filer_status=filer_status, registrant_links=links,
    )
    return {"contract": contract, "score": build_a_score(contract)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the A contract from persisted evidence")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    output = []
    for ticker in args.tickers:
        company = evidence_v3.load_company(ticker)
        if company is None:
            raise SystemExit(f"Ticker not found: {ticker}")
        result = evaluate_a(company[0], as_of)
        contract, score = result["contract"], result["score"]
        output.append({
            "ticker": ticker.upper(),
            "as_of": contract["as_of"],
            "inputs": {key: contract[key] for key in A_INPUT_KEYS},
            "diagnostics": contract["integrity"]["diagnostics"],
            "score": score["a_score_v1"]["normalized_score"],
            "class": score["a_score_v1"]["class"],
            "status": score["a_score_v1"]["status"],
            "usability": score["a_score_v1"]["usability"],
            "classic": score["a_classic"]["result"],
            "flags": score["a_flags"],
            "points": {name: item["points"] for name, item in score["a_score_v1"]["components"].items()},
        })
    print(json.dumps(output, indent=2, default=str))
    return output


if __name__ == "__main__":
    main()
