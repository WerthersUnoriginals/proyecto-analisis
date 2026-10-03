"""Market direction for M (m-market-direction-v1, spec 2026-10-03-market-direction-m-v1 §4).

Distribution days, rally attempts, follow-through days and a three-state
machine per index; the market state is the worse of the indexes. Thresholds
approved by the human on 2026-10-04 (rule B: distribution days only put an
uptrend under pressure; a correction needs an 8% drawdown).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from database.new_highs_v1 import STALE_BUSINESS_DAYS, business_days_between
from database.prices_v3 import AdjustedBar

MARKET_DIRECTION_VERSION = "m-market-direction-v1"
ANALYSIS_SESSIONS = 300
MA_LONG, MA_SHORT = 200, 50
DISTRIBUTION_DROP = Decimal("0.002")
DISTRIBUTION_WINDOW = 25
DISTRIBUTION_EXPIRY_GAIN = Decimal("0.05")
FOLLOW_THROUGH_MIN_DAY = 4
FOLLOW_THROUGH_GAIN = Decimal("0.0125")
PRESSURE_DISTRIBUTION_DAYS = 4
CORRECTION_DRAWDOWN = Decimal("0.08")
STATE_ORDER = ("CONFIRMED_UPTREND", "UPTREND_UNDER_PRESSURE", "CORRECTION")


@dataclass(frozen=True)
class IndexDirection:
    status: str
    state: str | None = None
    state_since: date | None = None
    last_follow_through_day: date | None = None
    active_distribution_days: int | None = None
    distribution_dates: tuple[date, ...] = ()
    rally_day: int | None = None
    pct_vs_ma50: Decimal | None = None
    pct_vs_ma200: Decimal | None = None
    last_bar_date: date | None = None


def distribution_flags(bars: Sequence[AdjustedBar]) -> list[bool]:
    flags = [False]
    for previous, current in zip(bars, bars[1:]):
        flags.append(current.close <= previous.close * (1 - DISTRIBUTION_DROP) and current.volume > previous.volume)
    return flags


def _active(bars: Sequence[AdjustedBar], flags: Sequence[bool], index: int, since: int = 0) -> list[int]:
    """Active distribution days at ``index``; a follow-through day at ``since`` clears earlier ones."""

    active = []
    for day in range(max(1, index - DISTRIBUTION_WINDOW + 1, since + 1), index + 1):
        if not flags[day]:
            continue
        later = [bar.close for bar in bars[day + 1:index + 1]]
        if not later or max(later) < bars[day].close * (1 + DISTRIBUTION_EXPIRY_GAIN):
            active.append(day)
    return active


def _ma(bars: Sequence[AdjustedBar], index: int, length: int) -> Decimal:
    window = bars[index - length + 1:index + 1]
    return sum((bar.close for bar in window), Decimal(0)) / len(window)


def analyze_index(bars: Sequence[AdjustedBar], as_of_date: date | None = None) -> IndexDirection:
    bars = sorted(bars, key=lambda bar: bar.bar_date)
    if as_of_date is not None:
        bars = [bar for bar in bars if bar.bar_date <= as_of_date]
    if len(bars) < ANALYSIS_SESSIONS + MA_LONG:
        return IndexDirection("INSUFFICIENT_HISTORY", last_bar_date=bars[-1].bar_date if bars else None)
    flags = distribution_flags(bars)
    start = len(bars) - ANALYSIS_SESSIONS
    state = "CONFIRMED_UPTREND" if bars[start].close > _ma(bars, start, MA_LONG) else "CORRECTION"
    since, last_ftd = bars[start].bar_date, None
    high = low = bars[start].close
    rally_day = 0
    uptrend_from = 0  # index of the follow-through day that started the current uptrend
    for index in range(start + 1, len(bars)):
        bar, previous = bars[index], bars[index - 1]
        active = len(_active(bars, flags, index, uptrend_from))
        if state == "CORRECTION":
            if bar.close < low:
                low, rally_day = bar.close, 0
            elif rally_day == 0 and bar.close > previous.close:
                rally_day = 1
            elif rally_day >= 1:
                rally_day += 1
            if (rally_day >= FOLLOW_THROUGH_MIN_DAY and bar.close >= previous.close * (1 + FOLLOW_THROUGH_GAIN)
                    and bar.volume > previous.volume):
                state, since, last_ftd, high, rally_day = "CONFIRMED_UPTREND", bar.bar_date, bar.bar_date, bar.close, 0
                uptrend_from = index
            continue
        high = max(high, bar.close)
        if bar.close <= high * (1 - CORRECTION_DRAWDOWN):
            state, since, low, rally_day, uptrend_from = "CORRECTION", bar.bar_date, bar.close, 0, 0
            continue
        new_state = "UPTREND_UNDER_PRESSURE" if active >= PRESSURE_DISTRIBUTION_DAYS else "CONFIRMED_UPTREND"
        if new_state != state:
            state, since = new_state, bar.bar_date
    last = len(bars) - 1
    active_days = _active(bars, flags, last, uptrend_from)
    status = "OK"
    if as_of_date is not None and business_days_between(bars[last].bar_date, as_of_date) > STALE_BUSINESS_DAYS:
        status = "STALE_PRICES"
    return IndexDirection(
        status=status, state=state, state_since=since, last_follow_through_day=last_ftd,
        active_distribution_days=len(active_days),
        distribution_dates=tuple(bars[day].bar_date for day in active_days),
        rally_day=rally_day if state == "CORRECTION" else None,
        pct_vs_ma50=(bars[last].close / _ma(bars, last, MA_SHORT) - 1) * 100,
        pct_vs_ma200=(bars[last].close / _ma(bars, last, MA_LONG) - 1) * 100,
        last_bar_date=bars[last].bar_date,
    )


def combine_states(states: Sequence[str | None]) -> str | None:
    """The worse state across indexes; unknown if any index is unknown (fail-closed)."""

    if any(state is None for state in states) or not states:
        return None
    return max(states, key=STATE_ORDER.index)


def breadth_above_ma50(series: Sequence[Sequence[AdjustedBar]], as_of_date: date) -> tuple[Decimal | None, int]:
    """Percentage of members whose last close is above their 50-session average, and members counted."""

    above = counted = 0
    for bars in series:
        bars = sorted((bar for bar in bars if bar.bar_date <= as_of_date), key=lambda bar: bar.bar_date)
        if len(bars) < MA_SHORT or business_days_between(bars[-1].bar_date, as_of_date) > STALE_BUSINESS_DAYS:
            continue
        counted += 1
        above += bars[-1].close > _ma(bars, len(bars) - 1, MA_SHORT)
    return (Decimal(above) / counted * 100 if counted else None), counted
