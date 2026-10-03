"""Evaluate the L contract for one company at ``as_of`` from persisted evidence.

Usage::

    python -m database.l_v3_runner NVDA AAPL [--as-of 2026-10-03T00:00:00+00:00]

The S&P 500 distribution is built once per ``as_of`` (``UniverseContext``) and
shared by every company evaluated at that time.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable

from l_score_v1 import build_l_score
from database import evidence_v3
from database.annual_v3 import build_annual_view
from database.c_v3_runner import load_split_reconciliation
from database.l_contract_v1 import L_INPUT_KEYS, build_l_contract_v1
from database.new_highs_v1 import compute_highs
from database.prices_v3 import AdjustedBar, build_price_series
from database.registrant_v3 import classify_filer
from database.relative_strength_v1 import (
    GroupStrength,
    UniverseMember,
    compute_group_strength,
    compute_rs_line,
    rs_rating,
    weighted_score,
)
from database.split_basis import effective_split_status
from database.universe_v1 import UNIVERSE_NAME, yahoo_ticker

BENCHMARK_TICKER = "SPY"


@dataclass(frozen=True)
class UniverseContext:
    as_of: datetime
    holdings_as_of: date | None
    members: tuple[UniverseMember, ...]
    excluded: int
    benchmark: tuple[AdjustedBar, ...]

    def scores_without(self, ticker: str) -> list[Decimal]:
        """Member scores, the subject itself left out by ticker."""

        return [member.score for member in self.members if member.ticker != ticker]


def build_universe_context(as_of: datetime, *, connection_factory: Callable | None = None) -> UniverseContext:
    """Score every universe member visible at ``as_of``.

    Members have no SEC facts, so no split reconciliation: a member whose own
    series shows a basis conflict or a suspected unrecorded split is left out
    and counted (spec §9).
    """

    holdings_as_of, source_tickers = evidence_v3.load_universe(UNIVERSE_NAME, as_of,
                                                               connection_factory=connection_factory)
    tickers = list(dict.fromkeys(filter(None, (yahoo_ticker(item) for item in source_tickers))))
    ids = evidence_v3.load_company_ids(tickers + [BENCHMARK_TICKER], connection_factory=connection_factory)
    member_ids = {ids[ticker]: ticker for ticker in tickers if ticker in ids}
    bars = evidence_v3.load_many_price_bars(list(member_ids) + ([ids[BENCHMARK_TICKER]] if BENCHMARK_TICKER in ids else []),
                                            as_of, connection_factory=connection_factory)
    profiles = evidence_v3.load_profiles(list(member_ids), as_of, connection_factory=connection_factory)
    members, excluded = [], len(tickers) - len(member_ids)
    for company_id, ticker in member_ids.items():
        series = build_price_series(bars.get(company_id, []), split_events=(), as_of=as_of)
        score = weighted_score(series.bars, as_of.date())
        if series.review_reasons or score.status != "OK":
            excluded += 1
            continue
        members.append(UniverseMember(ticker, profiles.get(company_id, (None, None))[0], score.value))
    benchmark = ()
    if BENCHMARK_TICKER in ids:
        benchmark = build_price_series(bars.get(ids[BENCHMARK_TICKER], []), split_events=(), as_of=as_of).bars
    return UniverseContext(as_of, holdings_as_of, tuple(members), excluded, tuple(benchmark))


_CONTEXTS: dict[datetime, UniverseContext] = {}


def _context(as_of: datetime, connection_factory) -> UniverseContext:
    if as_of not in _CONTEXTS:
        _CONTEXTS[as_of] = build_universe_context(as_of, connection_factory=connection_factory)
    return _CONTEXTS[as_of]


def evaluate_l(company_id: int, ticker: str, as_of: datetime, *, connection_factory: Callable | None = None,
               capture_loader: Callable | None = None, context: UniverseContext | None = None) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    context = context or _context(as_of, connection_factory)
    sec_facts = evidence_v3.load_sec_facts(company_id, as_of, connection_factory=connection_factory)
    splits = load_split_reconciliation(company_id, as_of, sec_facts, capture_loader=capture_loader)
    view = build_annual_view(sec_facts, split_events=splits.events, as_of=as_of)
    split_status, split_reasons = effective_split_status(
        splits, view_split_reasons=view.split_status_reasons, view_diagnostics=view.diagnostics,
        rejected=view.rejected_split_events,
    )
    applied = [event for event in splits.events if event not in view.rejected_split_events]
    series = build_price_series(evidence_v3.load_price_bars(company_id, as_of, connection_factory=connection_factory),
                                split_events=applied, as_of=as_of)
    score = weighted_score(series.bars, as_of.date())
    is_member = any(member.ticker == ticker for member in context.members)
    peers = context.scores_without(ticker)
    rating = rs_rating(score.value, peers) if score.status == "OK" and peers else None
    sic, sic_description = evidence_v3.load_profiles([company_id], as_of,
                                                     connection_factory=connection_factory).get(company_id, (None, None))
    group = (compute_group_strength(sic, score.value, context.members, subject_ticker=ticker)
             if score.value is not None else GroupStrength("NO_SCORE"))
    line = compute_rs_line(series.bars, context.benchmark, as_of.date())
    filer_status = classify_filer(
        evidence_v3.load_filing_forms(company_id, as_of, connection_factory=connection_factory), as_of,
    )
    contract = build_l_contract_v1(
        score, rating, group, line, series, splits, split_status=split_status, split_reasons=split_reasons,
        rejected_splits=view.rejected_split_events, universe_as_of=context.holdings_as_of,
        universe_scored=len(peers), universe_excluded=context.excluded,
        sic=sic, sic_description=sic_description, company_id=company_id, as_of=as_of,
        filer_status=filer_status, price_pct_below_high_52w=compute_highs(series.bars, as_of.date()).pct_below_high_52w,
    )
    return {"contract": contract, "score": build_l_score(contract)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the L contract from persisted evidence")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    output = []
    for ticker in args.tickers:
        company = evidence_v3.load_company(ticker)
        if company is None:
            raise SystemExit(f"Ticker not found: {ticker}")
        result = evaluate_l(company[0], ticker.upper(), as_of)
        contract, score = result["contract"], result["score"]
        output.append({
            "ticker": ticker.upper(),
            "as_of": contract["as_of"],
            "inputs": {key: contract[key] for key in L_INPUT_KEYS},
            "diagnostics": contract["integrity"]["diagnostics"],
            "score": score["l_score_v1"]["normalized_score"],
            "class": score["l_score_v1"]["class"],
            "usability": score["l_score_v1"]["usability"],
            "classic": score["l_classic"]["result"],
            "flags": score["l_flags"],
            "points": {name: item["points"] for name, item in score["l_score_v1"]["components"].items()},
        })
    print(json.dumps(output, indent=2, default=str))
    return output


if __name__ == "__main__":
    main()
