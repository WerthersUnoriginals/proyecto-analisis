"""Evaluate C v3 for one company at ``as_of`` from persisted evidence only.

Usage::

    python -m database.c_v3_runner NVDA [--as-of 2026-10-02T00:00:00+00:00]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from c_score_v1 import build_c_score as build_c_score_v12
from c_score_v13 import build_c_score
from database import evidence_v3
from database.c_contract_v3 import build_c_contract_v3
from database.quarterly_v3 import build_quarterly_view, six_years_before, yahoo_rows_from_snapshot
from database.split_basis import reconcile_split_events


def load_split_reconciliation(company_id: int, as_of: datetime, sec_facts, *, capture_loader: Callable | None = None):
    if capture_loader is None:
        from database.corporate_actions import load_latest_capture_as_of as capture_loader
    capture, events = capture_loader(company_id, as_of)
    provider_events = None
    if capture is not None and capture.acquisition_status == "SUCCESS" and capture.completeness_status == "COMPLETE":
        provider_events = [(event.event_date, event.split_ratio) for event in events]
    return reconcile_split_events(
        provider_events, sec_facts, window_start=six_years_before(as_of.date()), as_of=as_of.date(),
    )


def evaluate_c_v3(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None,
                  capture_loader: Callable | None = None) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    sec_facts = evidence_v3.load_sec_facts(company_id, as_of, connection_factory=connection_factory)
    yahoo_rows = yahoo_rows_from_snapshot(
        evidence_v3.load_yahoo_rows(company_id, as_of, connection_factory=connection_factory), as_of=as_of,
    )
    splits = load_split_reconciliation(company_id, as_of, sec_facts, capture_loader=capture_loader)
    view = build_quarterly_view(sec_facts, yahoo_rows, split_events=splits.events, as_of=as_of)
    contract = build_c_contract_v3(view, splits, company_id=company_id, as_of=as_of)
    score = build_c_score(contract)
    return {"contract": contract, "score": score, "score_v12": build_c_score_v12(contract), "evidence": {
        "sec_facts": len(sec_facts), "yahoo_rows": len(yahoo_rows),
    }}


def _summary(ticker: str, result: dict) -> dict:
    contract, score = result["contract"], result["score"]
    keys = (
        "latest_eps", "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
        "latest_revenue_yoy_pct", "previous_revenue_yoy_pct", "revenue_acceleration_pp",
        "eps_loss_to_profit", "data_integrity", "split_integrity_status",
    )
    inputs = contract["c_input_contract"]["inputs"]
    return {
        "ticker": ticker,
        "as_of": contract["as_of"],
        "inputs": {key: contract[key] for key in keys},
        "latest_eps_quarter": inputs["latest_eps"].get("fiscal_quarter"),
        "latest_eps_source": inputs["latest_eps"].get("source"),
        "eps_yoy_last_4": [round(item["value"], 2) for item in contract["eps_yoy_pct"][-4:]],
        "score": score["c_score_v1"]["normalized_score"],
        "score_model": score["c_score_v1"]["model_version"],
        "score_v12_reference": result["score_v12"]["c_score_v1"]["normalized_score"],
        "eps_status": score["c_score_v1"]["eps_status"],
        "class": score["c_score_v1"]["class"],
        "status": score["c_score_v1"]["status"],
        "usability": score["c_score_v1"]["usability"],
        "flags": score["c_flags"],
        "classic": score["c_classic"]["result"],
        "integrity": {key: contract["integrity"][key] for key in ("data_quality", "consistency", "shares_quality", "diagnostics")},
        "evidence": result["evidence"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate C v3 from persisted evidence")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    output = []
    for ticker in args.tickers:
        company = evidence_v3.load_company(ticker)
        if company is None:
            raise SystemExit(f"Ticker not found: {ticker}")
        output.append(_summary(ticker.upper(), evaluate_c_v3(company[0], as_of)))
    print(json.dumps(output, indent=2, default=str))
    return output


if __name__ == "__main__":
    main()
