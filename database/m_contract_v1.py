"""M input contract v1 (spec 2026-10-03-market-direction-m-v1 §5–§6)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping

from database.market_direction_v1 import MARKET_DIRECTION_VERSION, IndexDirection, combine_states
from database.prices_v3 import PRICE_SERIES_VERSION
from database.relative_strength_v1 import MIN_SCORED_UNIVERSE

CONTRACT_VERSION = "m-input-contract-v1"

M_INPUT_KEYS = (
    "market_state",
    "state_by_index",
    "state_since",
    "last_follow_through_day",
    "distribution_days",
    "rally_day",
    "pct_vs_ma50",
    "pct_vs_ma200",
    "breadth_pct_above_ma50",
    "breadth_members",
    "m_data_integrity",
)


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _date(value) -> str | None:
    return value.isoformat() if value else None


def build_m_contract_v1(
    indexes: Mapping[str, IndexDirection],
    *,
    series_review: Mapping[str, tuple[str, ...]],
    breadth_pct: Decimal | None,
    breadth_members: int,
    as_of: datetime,
) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    diagnostics: list[str] = []
    for ticker, result in indexes.items():
        if result.status != "OK":
            diagnostics.append(f"{ticker}:{result.status}")
        diagnostics.extend(f"{ticker}:{reason}" for reason in series_review.get(ticker, ()))
    state = combine_states([result.state for result in indexes.values()])
    worst = [ticker for ticker, result in indexes.items() if result.state == state]
    if any(result.status != "OK" for result in indexes.values()) or any(series_review.values()) or state is None:
        integrity = "REVIEW_REQUIRED"
    elif breadth_members < MIN_SCORED_UNIVERSE:
        integrity = "VERIFIED_WITH_PARTIAL_CORE_DATA"
        diagnostics.append("BREADTH_UNIVERSE_INCOMPLETE")
    else:
        integrity = "VERIFIED"

    since = [indexes[ticker].state_since for ticker in worst if indexes[ticker].state_since]
    follow_through = [result.last_follow_through_day for result in indexes.values() if result.last_follow_through_day]
    values = {
        "market_state": state,
        "state_by_index": {ticker: result.state for ticker, result in indexes.items()},
        "state_since": _date(max(since)) if since else None,
        "last_follow_through_day": _date(max(follow_through)) if follow_through else None,
        "distribution_days": {ticker: result.active_distribution_days for ticker, result in indexes.items()},
        "rally_day": {ticker: result.rally_day for ticker, result in indexes.items()},
        "pct_vs_ma50": {ticker: _number(result.pct_vs_ma50) for ticker, result in indexes.items()},
        "pct_vs_ma200": {ticker: _number(result.pct_vs_ma200) for ticker, result in indexes.items()},
        "breadth_pct_above_ma50": _number(breadth_pct),
        "breadth_members": breadth_members,
        "m_data_integrity": integrity,
    }
    provenance = {key: {} for key in M_INPUT_KEYS}
    provenance["distribution_days"] = {"dates": {ticker: [day.isoformat() for day in result.distribution_dates]
                                                 for ticker, result in indexes.items()}}
    provenance["market_state"] = {"worst_indexes": worst,
                                  "last_bar_dates": {t: _date(r.last_bar_date) for t, r in indexes.items()}}
    provenance["m_data_integrity"] = {"reasons": list(dict.fromkeys(diagnostics))}
    as_of_text = as_of.isoformat()
    return {
        **values,
        "as_of": as_of_text,
        "m_input_contract": {
            "version": CONTRACT_VERSION,
            "calculation_version": MARKET_DIRECTION_VERSION,
            "price_series_version": PRICE_SERIES_VERSION,
            "as_of": as_of_text,
            "inputs": {key: {"value": values[key], **provenance[key]} for key in M_INPUT_KEYS},
        },
        "integrity": {"m_data_integrity": integrity, "diagnostics": list(dict.fromkeys(diagnostics))},
    }
