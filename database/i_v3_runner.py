"""Evaluate the I contract for one company at ``as_of`` from persisted evidence.

Usage::

    python -m database.i_v3_runner NVDA RDDT [--as-of 2026-10-03T00:00:00+00:00]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from i_score_v1 import build_i_score
from database import evidence_v3
from database.annual_v3 import build_annual_view
from database.c_v3_runner import load_split_reconciliation
from database.i_contract_v1 import I_INPUT_KEYS, build_i_contract_v1
from database.registrant_v3 import classify_filer
from database.split_basis import effective_split_status
from database.sponsorship_v1 import Sponsorship, complete_quarters, compute_sponsorship
from database.supply_demand_v1 import S_ANNUAL_METRICS, S_INSTANT_METRICS, compute_share_supply


def evaluate_i(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None,
               capture_loader: Callable | None = None) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    sec_facts = evidence_v3.load_sec_facts(company_id, as_of, connection_factory=connection_factory)
    splits = load_split_reconciliation(company_id, as_of, sec_facts, capture_loader=capture_loader)
    # The share supply view of S provides the denominator of the ownership.
    view = build_annual_view(sec_facts, split_events=splits.events, as_of=as_of,
                             metrics=S_ANNUAL_METRICS, instant_metrics=S_INSTANT_METRICS)
    split_status, split_reasons = effective_split_status(
        splits, view_split_reasons=view.split_status_reasons, view_diagnostics=view.diagnostics,
        rejected=view.rejected_split_events,
    )
    applied = [event for event in splits.events if event not in view.rejected_split_events]
    cusip, source = evidence_v3.load_company_cusip(company_id, as_of, connection_factory=connection_factory)
    if cusip:
        filings, holdings = evidence_v3.load_13f(cusip, complete_quarters(as_of.date(), 5), as_of,
                                                 connection_factory=connection_factory)
        sponsorship = compute_sponsorship(filings, holdings, cusip, as_of=as_of,
                                          shares_outstanding=compute_share_supply(view).diluted_shares_latest,
                                          split_events=applied)
    else:
        sponsorship = Sponsorship("NO_CUSIP")
    filer_status = classify_filer(
        evidence_v3.load_filing_forms(company_id, as_of, connection_factory=connection_factory), as_of,
    )
    contract = build_i_contract_v1(
        sponsorship, splits, cusip=cusip, cusip_source=source, split_status=split_status,
        split_reasons=split_reasons, rejected_splits=view.rejected_split_events, company_id=company_id,
        as_of=as_of, filer_status=filer_status,
    )
    return {"contract": contract, "score": build_i_score(contract)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the I contract from persisted evidence")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    output = []
    for ticker in args.tickers:
        company = evidence_v3.load_company(ticker)
        if company is None:
            raise SystemExit(f"Ticker not found: {ticker}")
        result = evaluate_i(company[0], as_of)
        contract, score = result["contract"], result["score"]
        output.append({
            "ticker": ticker.upper(),
            "as_of": contract["as_of"],
            "inputs": {key: contract[key] for key in I_INPUT_KEYS},
            "diagnostics": contract["integrity"]["diagnostics"],
            "score": score["i_score_v1"]["normalized_score"],
            "class": score["i_score_v1"]["class"],
            "usability": score["i_score_v1"]["usability"],
            "classic": score["i_classic"]["result"],
            "flags": score["i_flags"],
            "points": {name: item["points"] for name, item in score["i_score_v1"]["components"].items()},
        })
    print(json.dumps(output, indent=2, default=str))
    return output


if __name__ == "__main__":
    main()
