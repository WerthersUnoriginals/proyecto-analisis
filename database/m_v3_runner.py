"""Evaluate the market-wide M contract at ``as_of`` from persisted evidence.

Usage::

    python -m database.m_v3_runner [--as-of 2026-10-03T00:00:00+00:00]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from m_score_v1 import build_m_score
from database import evidence_v3
from database.m_contract_v1 import M_INPUT_KEYS, build_m_contract_v1
from database.market_direction_v1 import IndexDirection, analyze_index, breadth_above_ma50
from database.market_v1 import BENCHMARKS
from database.prices_v3 import build_price_series
from database.universe_v1 import UNIVERSE_NAME, yahoo_ticker

_RESULTS: dict[datetime, dict] = {}


def evaluate_m(as_of: datetime, *, connection_factory: Callable | None = None) -> dict:
    """M at ``as_of``; computed once per ``as_of`` and shared by every company."""

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if as_of in _RESULTS:
        return _RESULTS[as_of]
    _, source_tickers = evidence_v3.load_universe(UNIVERSE_NAME, as_of, connection_factory=connection_factory)
    members = list(dict.fromkeys(filter(None, (yahoo_ticker(item) for item in source_tickers))))
    ids = evidence_v3.load_company_ids(members + list(BENCHMARKS), connection_factory=connection_factory)
    bars = evidence_v3.load_many_price_bars(list(ids.values()), as_of, connection_factory=connection_factory)

    indexes, review = {}, {}
    for ticker in BENCHMARKS:
        if ticker not in ids:
            indexes[ticker], review[ticker] = IndexDirection("NO_PRICE_EVIDENCE"), ()
            continue
        series = build_price_series(bars[ids[ticker]], split_events=(), as_of=as_of)
        indexes[ticker] = analyze_index(series.bars, as_of.date())
        review[ticker] = series.review_reasons
    member_series = []
    for ticker in members:
        if ticker in ids:
            series = build_price_series(bars[ids[ticker]], split_events=(), as_of=as_of)
            if not series.review_reasons:
                member_series.append(series.bars)
    breadth, counted = breadth_above_ma50(member_series, as_of.date())
    contract = build_m_contract_v1(indexes, series_review=review, breadth_pct=breadth, breadth_members=counted,
                                   as_of=as_of)
    _RESULTS[as_of] = {"contract": contract, "score": build_m_score(contract)}
    return _RESULTS[as_of]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate market direction (M) from persisted evidence")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    result = evaluate_m(as_of)
    contract, score = result["contract"], result["score"]
    output = {
        "as_of": contract["as_of"],
        "inputs": {key: contract[key] for key in M_INPUT_KEYS},
        "diagnostics": contract["integrity"]["diagnostics"],
        "distribution_dates": contract["m_input_contract"]["inputs"]["distribution_days"]["dates"],
        "score": score["m_score_v1"]["normalized_score"],
        "class": score["m_score_v1"]["class"],
        "usability": score["m_score_v1"]["usability"],
        "classic": score["m_classic"]["result"],
        "points": {name: item["points"] for name, item in score["m_score_v1"]["components"].items()},
    }
    print(json.dumps(output, indent=2, default=str))
    return output


if __name__ == "__main__":
    main()
