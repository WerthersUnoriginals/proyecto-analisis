"""Position of the price relative to its highs (n-highs-v1, spec 2026-10-03 §6).

N measures highs only; bases, pivots and breakout volume belong to the
technical layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from database.prices_v3 import AdjustedBar

NEW_HIGHS_VERSION = "n-highs-v1"
SESSIONS_52W = 252
RECENT_HIGH_SESSIONS = 20
STALE_BUSINESS_DAYS = 5
GAP_BUSINESS_DAYS = 5
LONG_HIGH_YEARS = 5
# The 5-year window must start no later than this after its nominal start.
LONG_HIGH_COVERAGE_DAYS = 10


@dataclass(frozen=True)
class HighsResult:
    status: str
    last_bar_date: date | None = None
    close_last: Decimal | None = None
    high_52w: Decimal | None = None
    pct_below_high_52w: Decimal | None = None
    new_high_recent: bool | None = None
    sessions_since_high_52w: int | None = None
    high_5y: Decimal | None = None
    pct_below_high_5y: Decimal | None = None
    sessions_available: int = 0
    diagnostics: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    version: str = NEW_HIGHS_VERSION


def business_days_between(start: date, end: date) -> int:
    """Weekdays in ``(start, end]``."""

    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # February 29
        return day.replace(year=day.year - years, day=28)


def _pct_below(close: Decimal, high: Decimal) -> Decimal:
    return (1 - close / high) * 100


def compute_highs(bars: Sequence[AdjustedBar], as_of_date: date) -> HighsResult:
    bars = sorted((item for item in bars if item.bar_date <= as_of_date), key=lambda item: item.bar_date)
    if not bars:
        return HighsResult(status="NO_PRICE_EVIDENCE", diagnostics=("NO_PRICE_EVIDENCE",))
    last = bars[-1]
    diagnostics: list[str] = []
    review: list[str] = []
    stale = business_days_between(last.bar_date, as_of_date) > STALE_BUSINESS_DAYS
    if stale:
        diagnostics.append("STALE_PRICES")
    base = dict(last_bar_date=last.bar_date, close_last=last.close, sessions_available=len(bars))
    if len(bars) < SESSIONS_52W:
        diagnostics.append("INSUFFICIENT_PRICE_HISTORY")
        return HighsResult(status="STALE_PRICES" if stale else "INSUFFICIENT_HISTORY",
                           diagnostics=tuple(diagnostics), **base)

    window = bars[-SESSIONS_52W:]
    for previous, current in zip(window, window[1:]):
        if business_days_between(previous.bar_date, current.bar_date) > GAP_BUSINESS_DAYS:
            review.append("PRICE_GAP")
            diagnostics.append(f"PRICE_GAP:{previous.bar_date.isoformat()}..{current.bar_date.isoformat()}")
    high_52w = max(item.high for item in window)
    latest_at_high = max(index for index, item in enumerate(window) if item.high == high_52w)
    sessions_since = len(window) - 1 - latest_at_high

    long_start = _years_before(last.bar_date, LONG_HIGH_YEARS)
    high_5y = pct_5y = None
    if bars[0].bar_date <= long_start + timedelta(days=LONG_HIGH_COVERAGE_DAYS):
        high_5y = max(item.high for item in bars if item.bar_date > long_start)
        pct_5y = _pct_below(last.close, high_5y)
    else:
        diagnostics.append("HIGH_5Y_INSUFFICIENT_HISTORY")

    return HighsResult(
        status="STALE_PRICES" if stale else "OK",
        high_52w=high_52w,
        pct_below_high_52w=_pct_below(last.close, high_52w),
        new_high_recent=sessions_since < RECENT_HIGH_SESSIONS,
        sessions_since_high_52w=sessions_since,
        high_5y=high_5y,
        pct_below_high_5y=pct_5y,
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        review_reasons=tuple(dict.fromkeys(review)),
        **base,
    )
