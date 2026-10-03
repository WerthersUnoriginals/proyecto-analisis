"""Port of the human's "Score Fase de Entrada v2" TradingView indicator (entry-phase-v2).

Reference: docs/reference/score_fase_entrada_v2.pine; spec
2026-10-04-entry-phase-technical-v2. Daily bars, lookbacks as on the daily
chart (one "day" = one session, H1). Pine built-ins are reproduced with
floats, as TradingView computes them; prices come from the N series on the
``as_of`` split basis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from database.new_highs_v1 import STALE_BUSINESS_DAYS, business_days_between
from database.prices_v3 import AdjustedBar

ENTRY_PHASE_VERSION = "entry-phase-v2"
MA_FAST, MA_SLOW = 50, 200
RANGE_BARS, PERCENTILE_BARS, AMPLITUDE_PERCENTILE_MAX = 42, 365, 40.0
ATR_LEN, ATR_LOOKBACK = 14, 14
RANGE_POSITION_MIN = 0.66
VOL_SHORT, VOL_LONG, VOL_KEEP = 5, 20, 0.8
TRIGGER_BARS, TRIGGER_MAX_PCT = 365, 3.0
CHANNEL_BARS, CHANNEL_R2_MIN, CHANNEL_STDEVS = 60, 0.7, 2.0
RSI_LEN, MOMENTUM_BARS, RSI_MARGIN, NEAR_HIGH_PCT = 14, 60, 5.0, 1.0
HISTORY_SESSIONS = 10
MIN_BARS = RANGE_BARS + PERCENTILE_BARS + HISTORY_SESSIONS + 13  # 430


# --------------------------------------------------------------------------
# Pine built-ins (series in, series out; None where Pine would give na)
# --------------------------------------------------------------------------

def sma(values: Sequence[float | None], length: int) -> list[float | None]:
    out: list[float | None] = []
    for index in range(len(values)):
        window = values[index - length + 1:index + 1] if index >= length - 1 else []
        out.append(sum(window) / length if window and None not in window else None)
    return out


def highest(values: Sequence[float | None], length: int) -> list[float | None]:
    out = []
    for index in range(len(values)):
        window = values[max(0, index - length + 1):index + 1]
        valid = [value for value in window if value is not None]
        out.append(max(valid) if index >= length - 1 and len(valid) == length else None)
    return out


def lowest(values: Sequence[float], length: int) -> list[float | None]:
    return [min(values[index - length + 1:index + 1]) if index >= length - 1 else None
            for index in range(len(values))]


def percentrank(values: Sequence[float | None], length: int) -> list[float | None]:
    """% of the previous ``length`` values (current excluded) that are <= the current one."""

    out: list[float | None] = []
    for index, current in enumerate(values):
        previous = values[index - length:index] if index >= length else []
        if current is None or not previous or None in previous:
            out.append(None)
            continue
        out.append(sum(1 for value in previous if value <= current) / length * 100.0)
    return out


def rma(values: Sequence[float | None], length: int) -> list[float | None]:
    """Wilder's moving average, seeded with the SMA of the first ``length`` valid values."""

    out: list[float | None] = []
    seed: list[float] = []
    previous = None
    for value in values:
        if value is None:
            out.append(None)
            continue
        if previous is None:
            seed.append(value)
            if len(seed) == length:
                previous = sum(seed) / length
                out.append(previous)
            else:
                out.append(None)
            continue
        previous = (previous * (length - 1) + value) / length
        out.append(previous)
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> list[float]:
    out = [high[0] - low[0]]
    for index in range(1, len(close)):
        previous = close[index - 1]
        out.append(max(high[index] - low[index], abs(high[index] - previous), abs(low[index] - previous)))
    return out


def rsi(close: Sequence[float], length: int) -> list[float | None]:
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    for previous, current in zip(close, close[1:]):
        gains.append(max(current - previous, 0.0))
        losses.append(max(previous - current, 0.0))
    up, down = rma(gains, length), rma(losses, length)
    out: list[float | None] = []
    for u, d in zip(up, down):
        if u is None or d is None:
            out.append(None)
        elif d == 0:
            out.append(100.0)
        elif u == 0:
            out.append(0.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + u / d))
    return out


def _regression(window: Sequence[float]) -> tuple[float, float]:
    """(slope, intercept) of the least-squares line over x = 0..n-1."""

    n = len(window)
    mean_x, mean_y = (n - 1) / 2.0, sum(window) / n
    sxx = sum((x - mean_x) ** 2 for x in range(n))
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(window))
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


def linreg(window: Sequence[float], offset: int) -> float:
    slope, intercept = _regression(window)
    return intercept + slope * (len(window) - 1 - offset)


def correlation_with_index(window: Sequence[float]) -> float | None:
    n = len(window)
    mean_x, mean_y = (n - 1) / 2.0, sum(window) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(window))
    sxx = sum((x - mean_x) ** 2 for x in range(n))
    syy = sum((y - mean_y) ** 2 for y in window)
    return None if syy == 0 else sxy / math.sqrt(sxx * syy)


def stdev(window: Sequence[float]) -> float:
    mean = sum(window) / len(window)
    return math.sqrt(sum((value - mean) ** 2 for value in window) / len(window))


# --------------------------------------------------------------------------
# Indicator
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EntryPhase:
    status: str
    last_bar_date: date | None = None
    layer1_ok: bool | None = None
    conditions: dict = field(default_factory=dict)
    score: int | None = None
    score_final: int | None = None
    setup_type: str | None = None
    momentum: str | None = None
    values: dict = field(default_factory=dict)
    score_history: tuple = ()
    sessions_since_score_4: int | None = None


def _score_at(i, close, ma50, ma200, amp_rank, atr, range_high, range_low, vol_short, vol_long, ref_high):
    layer1 = close[i] > ma200[i] and ma50[i] > ma200[i]
    span = range_high[i] - range_low[i]
    position = (close[i] - range_low[i]) / span if span != 0 else 0.0
    distance = (ref_high[i] - close[i]) / close[i] * 100.0
    conditions = {
        "amplitude_percentile": amp_rank[i] is not None and amp_rank[i] <= AMPLITUDE_PERCENTILE_MAX,
        "atr_contraction": atr[i - ATR_LOOKBACK] is not None and atr[i] < atr[i - ATR_LOOKBACK],
        "range_position": position > RANGE_POSITION_MIN,
        "volume_sustained": vol_short[i] >= vol_long[i] * VOL_KEEP,
        "trigger_distance": 0.0 <= distance < TRIGGER_MAX_PCT,
    }
    score = sum(conditions.values())
    return layer1, conditions, score, (score if layer1 else 0), position, distance


def compute_entry_phase(bars: Sequence[AdjustedBar], as_of_date: date) -> EntryPhase:
    bars = sorted((bar for bar in bars if bar.bar_date <= as_of_date), key=lambda bar: bar.bar_date)
    if len(bars) < MIN_BARS:
        return EntryPhase("INSUFFICIENT_HISTORY", bars[-1].bar_date if bars else None)
    high = [float(bar.high) for bar in bars]
    low = [float(bar.low) for bar in bars]
    close = [float(bar.close) for bar in bars]
    volume = [float(bar.volume) for bar in bars]

    ma50, ma200 = sma(close, MA_FAST), sma(close, MA_SLOW)
    range_high, range_low = highest(high, RANGE_BARS), lowest(low, RANGE_BARS)
    amplitude = [None if h is None else (h - l) / l * 100.0 for h, l in zip(range_high, range_low)]
    amp_rank = percentrank(amplitude, PERCENTILE_BARS)
    atr = rma(true_range(high, low, close), ATR_LEN)
    vol_short, vol_long = sma(volume, VOL_SHORT), sma(volume, VOL_LONG)
    ref_high = highest(high, TRIGGER_BARS)
    args = (close, ma50, ma200, amp_rank, atr, range_high, range_low, vol_short, vol_long, ref_high)

    last = len(bars) - 1
    layer1, conditions, score, score_final, position, distance = _score_at(last, *args)
    history = tuple(_score_at(index, *args)[3] for index in range(last - HISTORY_SESSIONS + 1, last + 1))
    since = None
    for back, index in enumerate(range(last, MIN_BARS - 1, -1)):
        if _score_at(index, *args)[3] >= 4:
            since = back
            break

    window = close[-CHANNEL_BARS:]
    # Pine: linreg(close, n, 0) - linreg(close, n, 1) on the same window is the regression slope.
    slope = linreg(window, 0) - linreg(window, 1)
    r = correlation_with_index(window)
    r2 = None if r is None else r * r
    channel_valid = r2 is not None and r2 >= CHANNEL_R2_MIN and slope > 0
    mid, deviation = linreg(window, 0), stdev(window)
    if channel_valid:
        setup = "TREND_CHANNEL"
    elif conditions["amplitude_percentile"]:
        setup = "COMPRESSION_BASE"
    else:
        setup = "NO_CLEAR_STRUCTURE"

    rsi_values = rsi(close, RSI_LEN)
    near_high = close[last] >= max(high[-MOMENTUM_BARS:]) * (1 - NEAR_HIGH_PCT / 100.0)
    rsi_window = [value for value in rsi_values[-MOMENTUM_BARS:] if value is not None]
    rsi_near_max = rsi_values[last] is not None and rsi_values[last] >= max(rsi_window) - RSI_MARGIN
    momentum = "N/A" if not near_high else ("CONFIRMED" if rsi_near_max else "DIVERGENT")

    status = "STALE_PRICES" if business_days_between(bars[last].bar_date, as_of_date) > STALE_BUSINESS_DAYS else "OK"
    return EntryPhase(
        status=status, last_bar_date=bars[last].bar_date, layer1_ok=layer1, conditions=conditions, score=score,
        score_final=score_final, setup_type=setup, momentum=momentum,
        values={
            "close": close[last], "ma50": ma50[last], "ma200": ma200[last],
            "range_high": range_high[last], "range_low": range_low[last], "amplitude_pct": amplitude[last],
            "amplitude_percentile": amp_rank[last], "atr": atr[last], "atr_past": atr[last - ATR_LOOKBACK],
            "range_position": position, "volume_sma5": vol_short[last], "volume_sma20": vol_long[last],
            "reference_high": ref_high[last], "trigger_distance_pct": distance,
            "channel_r2": r2, "channel_slope": slope, "channel_upper": mid + CHANNEL_STDEVS * deviation,
            "channel_lower": mid - CHANNEL_STDEVS * deviation, "rsi": rsi_values[last],
        },
        score_history=history, sessions_since_score_4=since,
    )
