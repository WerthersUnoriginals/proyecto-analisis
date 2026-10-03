"""Evaluate the S contract for one company at ``as_of`` from persisted evidence.

Usage::

    python -m database.s_v3_runner AAPL NVDA [--as-of 2026-10-03T00:00:00+00:00]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from s_score_v1 import build_s_score
from database import evidence_v3
from database.annual_v3 import build_annual_view
from database.c_v3_runner import load_split_reconciliation
from database.prices_v3 import build_price_series
from database.registrant_v3 import classify_filer
from database.s_contract_v1 import S_INPUT_KEYS, build_s_contract_v1
from database.split_basis import effective_split_status
from database.supply_demand_v1 import (
    S_ANNUAL_METRICS,
    S_INSTANT_METRICS,
    compute_leverage,
    compute_share_supply,
    compute_volume_demand,
)


def evaluate_s(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None,
               capture_loader: Callable | None = None) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    sec_facts = evidence_v3.load_sec_facts(company_id, as_of, connection_factory=connection_factory)
    splits = load_split_reconciliation(company_id, as_of, sec_facts, capture_loader=capture_loader)
    view = build_annual_view(sec_facts, split_events=splits.events, as_of=as_of,
                             metrics=S_ANNUAL_METRICS, instant_metrics=S_INSTANT_METRICS)
    split_status, split_reasons = effective_split_status(
        splits,
        view_split_reasons=view.split_status_reasons,
        view_diagnostics=view.diagnostics,
        rejected=view.rejected_split_events,
    )
    applied = [event for event in splits.events if event not in view.rejected_split_events]
    bars = evidence_v3.load_price_bars(company_id, as_of, connection_factory=connection_factory)
    series = build_price_series(bars, split_events=applied, as_of=as_of)
    filer_status = classify_filer(
        evidence_v3.load_filing_forms(company_id, as_of, connection_factory=connection_factory), as_of,
    )
    links = tuple(evidence_v3.load_registrant_links(company_id, as_of, connection_factory=connection_factory))
    contract = build_s_contract_v1(
        view, compute_share_supply(view), compute_leverage(view),
        compute_volume_demand(series.bars, as_of.date()), series, splits,
        split_status=split_status, split_reasons=split_reasons, rejected_splits=view.rejected_split_events,
        company_id=company_id, as_of=as_of, filer_status=filer_status, registrant_links=links,
    )
    return {"contract": contract, "score": build_s_score(contract)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the S contract from persisted evidence")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    output = []
    for ticker in args.tickers:
        company = evidence_v3.load_company(ticker)
        if company is None:
            raise SystemExit(f"Ticker not found: {ticker}")
        result = evaluate_s(company[0], as_of)
        contract, score = result["contract"], result["score"]
        output.append({
            "ticker": ticker.upper(),
            "as_of": contract["as_of"],
            "inputs": {key: contract[key] for key in S_INPUT_KEYS},
            "diagnostics": contract["integrity"]["diagnostics"],
            "score": score["s_score_v1"]["normalized_score"],
            "class": score["s_score_v1"]["class"],
            "status": score["s_score_v1"]["status"],
            "usability": score["s_score_v1"]["usability"],
            "classic": score["s_classic"]["result"],
            "flags": score["s_flags"],
            "points": {name: item["points"] for name, item in score["s_score_v1"]["components"].items()},
        })
    print(json.dumps(output, indent=2, default=str))
    return output


if __name__ == "__main__":
    main()
