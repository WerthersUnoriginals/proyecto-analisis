"""Relative strength for L (l-relative-strength-v1, spec 2026-10-03-leader-laggard-l-v1 §5)."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Sequence

from database.new_highs_v1 import RECENT_HIGH_SESSIONS, SESSIONS_52W, STALE_BUSINESS_DAYS, business_days_between
from database.prices_v3 import AdjustedBar

RELATIVE_STRENGTH_VERSION = "l-relative-strength-v1"
# Latest quarter weighted double (IBD style): 0.4 C/C63 + 0.2 C/C126 + 0.2 C/C189 + 0.2 C/C252.
WEIGHTS = ((63, Decimal("0.4")), (126, Decimal("0.2")), (189, Decimal("0.2")), (252, Decimal("0.2")))
MIN_SCORED_UNIVERSE = 400
MIN_GROUP_MEMBERS = 3


@dataclass(frozen=True)
class WeightedScore:
    status: str
    value: Decimal | None = None
    last_bar_date: date | None = None


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    sic: str | None
    score: Decimal


@dataclass(frozen=True)
class GroupStrength:
    status: str
    group_key: str | None = None
    group_size: int = 0
    group_rank_pct: Decimal | None = None
    rank_in_group_pct: Decimal | None = None
    groups_ranked: int = 0


@dataclass(frozen=True)
class RsLine:
    status: str
    sessions: int = 0
    pct_below_high_52w: Decimal | None = None
    new_high_recent: bool | None = None
    at_high: bool | None = None


def _sorted_bars(bars: Sequence[AdjustedBar], as_of_date: date) -> list[AdjustedBar]:
    return sorted((item for item in bars if item.bar_date <= as_of_date), key=lambda item: item.bar_date)


def weighted_score(bars: Sequence[AdjustedBar], as_of_date: date) -> WeightedScore:
    bars = _sorted_bars(bars, as_of_date)
    if len(bars) < WEIGHTS[-1][0] + 1:
        return WeightedScore("INSUFFICIENT_HISTORY")
    last = bars[-1]
    if business_days_between(last.bar_date, as_of_date) > STALE_BUSINESS_DAYS:
        return WeightedScore("STALE_PRICES", last_bar_date=last.bar_date)
    value = sum((weight * last.close / bars[-1 - back].close for back, weight in WEIGHTS), Decimal(0))
    return WeightedScore("OK", value, last.bar_date)


def _percentile(value: Decimal, ordered: Sequence[Decimal]) -> Decimal:
    """Share of ``ordered`` (sorted) below ``value``, ties counting half, in [0, 1]."""

    below = bisect.bisect_left(ordered, value)
    ties = bisect.bisect_right(ordered, value) - below
    return (Decimal(below) + Decimal(ties) / 2) / Decimal(len(ordered))


def rs_rating(score: Decimal, universe: Sequence[Decimal], *, exclude_self: bool = False) -> int:
    ordered = sorted(universe)
    if exclude_self:
        ordered.pop(bisect.bisect_left(ordered, score))
    return min(99, 1 + int(_percentile(score, ordered) * 99))


def sic_group(sic: str | None) -> str | None:
    return sic[:2] if sic and len(sic) >= 2 else None


def compute_group_strength(
    sic: str | None, score: Decimal, members: Sequence[UniverseMember], *, subject_ticker: str,
) -> GroupStrength:
    key = sic_group(sic)
    if key is None:
        return GroupStrength("NO_SIC")
    groups: dict[str, list[UniverseMember]] = {}
    for member in members:
        member_key = sic_group(member.sic)
        if member_key is not None:
            groups.setdefault(member_key, []).append(member)
    ranked = {name: median(item.score for item in items) for name, items in groups.items()
              if len(items) >= MIN_GROUP_MEMBERS}
    own = groups.get(key, [])
    if key not in ranked or len(ranked) < 2:
        return GroupStrength("GROUP_NOT_RANKED", key, len(own), groups_ranked=len(ranked))
    others = sorted(value for name, value in ranked.items() if name != key)
    below = bisect.bisect_left(others, ranked[key]) + (bisect.bisect_right(others, ranked[key])
                                                       - bisect.bisect_left(others, ranked[key])) / 2
    group_rank = Decimal(below) / Decimal(len(others)) * 100
    peers = sorted(item.score for item in own if item.ticker != subject_ticker)
    in_group = _percentile(score, peers) * 100 if peers else None
    return GroupStrength("OK", key, len(own), group_rank, in_group, len(ranked))


def compute_rs_line(stock: Sequence[AdjustedBar], market: Sequence[AdjustedBar], as_of_date: date) -> RsLine:
    market_close = {item.bar_date: item.close for item in market if item.bar_date <= as_of_date}
    line = [(item.bar_date, item.close / market_close[item.bar_date])
            for item in _sorted_bars(stock, as_of_date) if item.bar_date in market_close]
    window = line[-SESSIONS_52W:]
    if len(window) < SESSIONS_52W:
        return RsLine("INSUFFICIENT_HISTORY", len(window))
    status = "STALE_PRICES" if business_days_between(window[-1][0], as_of_date) > STALE_BUSINESS_DAYS else "OK"
    high = max(value for _, value in window)
    last = window[-1][1]
    recent_high = max(value for _, value in window[-RECENT_HIGH_SESSIONS:])
    return RsLine(status, len(window), (1 - last / high) * 100, recent_high == high, last == high)
