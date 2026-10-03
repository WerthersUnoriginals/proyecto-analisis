"""Daily price evidence and the point-in-time price series (price-series-v1).

Yahoo has no unadjusted history: every bar it returns is on the split basis of
the observation date (verified on NVDA's 2024 10:1 split, spec
2026-10-03-new-highs-n-v1 §2). Bars are stored literally, and on read each bar
is converted from its observation basis to the basis in force at ``as_of``
with the SEC-verified split events of ``split-basis-v1``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from database.split_basis import (
    SUSPECTED_RATIO_TOLERANCE,
    SplitEvent,
    basis_factor,
    basis_uncertain,
    suspected_split_ratio,
)

PRICE_SERIES_VERSION = "price-series-v1"
DEFAULT_EXCHANGE_TIMEZONE = "America/New_York"
SUPPORTED_CURRENCY = "USD"
# Yahoo serves float32 prices (95.20 arrives as 95.19999694824219); micro-units
# keep every quoted digit and make repeated observations compare equal.
PRICE_QUANTUM = Decimal("0.000001")
# A same-day bar is final once the session and its after-hours prints are over.
FINAL_BAR_LOCAL_HOUR = 20
PRICE_FIELDS = ("Open", "High", "Low", "Close", "Adj Close")


@dataclass(frozen=True)
class PriceBar:
    """One literal daily bar on the split basis of its observation date."""

    bar_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal
    volume: int
    currency: str
    observed_at: datetime
    id: int | None = None


@dataclass(frozen=True)
class AdjustedBar:
    bar_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    basis_factor: Decimal
    observed_at: datetime
    raw_id: int | None = None


@dataclass(frozen=True)
class PriceSeries:
    bars: tuple[AdjustedBar, ...]
    diagnostics: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    version: str = PRICE_SERIES_VERSION


def observation_date(observed_at: datetime, exchange_timezone: str = DEFAULT_EXCHANGE_TIMEZONE) -> date:
    return observed_at.astimezone(ZoneInfo(exchange_timezone)).date()


def is_final_bar(bar_date: date, observed_at: datetime, exchange_timezone: str) -> bool:
    local = observed_at.astimezone(ZoneInfo(exchange_timezone))
    if bar_date < local.date():
        return True
    return bar_date == local.date() and local.hour >= FINAL_BAR_LOCAL_HOUR


def _price(value) -> Decimal | None:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return Decimal(repr(number)).quantize(PRICE_QUANTUM)


def _rejection(values: Mapping) -> str | None:
    prices = [_price(values[name]) for name in PRICE_FIELDS]
    volume = float(values["Volume"])
    if any(item is None for item in prices) or math.isnan(volume):
        return "NAN_VALUE"
    open_, high, low, close, adj_close = prices
    if min(prices) <= 0:
        return "NON_POSITIVE_PRICE"
    if not (low <= min(open_, close) and max(open_, close) <= high):
        return "OHLC_INCONSISTENT"
    if volume < 0:
        return "NEGATIVE_VOLUME"
    return None


def bars_from_rows(
    rows: Iterable[tuple[date, Mapping]],
    *,
    observed_at: datetime,
    exchange_timezone: str,
    currency: str,
) -> tuple[list[PriceBar], dict]:
    """Final, valid bars from provider rows, plus what was dropped and why."""

    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    bars: list[PriceBar] = []
    partial = 0
    rejected: list[dict] = []
    for bar_date, values in rows:
        if not is_final_bar(bar_date, observed_at, exchange_timezone):
            partial += 1
            continue
        reason = _rejection(values)
        if reason is not None:
            rejected.append({"date": bar_date.isoformat(), "reason": reason})
            continue
        open_, high, low, close, adj_close = (_price(values[name]) for name in PRICE_FIELDS)
        bars.append(PriceBar(
            bar_date=bar_date, open=open_, high=high, low=low, close=close, adj_close=adj_close,
            volume=int(values["Volume"]), currency=currency, observed_at=observed_at,
        ))
    return bars, {"partial_bars": partial, "rejected_bars": rejected}


def _adjust(bar: PriceBar, events: Sequence[SplitEvent], as_of_date: date, exchange_timezone: str) -> AdjustedBar:
    factor = basis_factor(events, observation_date(bar.observed_at, exchange_timezone), as_of_date)
    return AdjustedBar(
        bar_date=bar.bar_date, open=bar.open / factor, high=bar.high / factor, low=bar.low / factor,
        close=bar.close / factor, volume=Decimal(bar.volume) * factor, basis_factor=factor,
        observed_at=bar.observed_at, raw_id=bar.id,
    )


def build_price_series(
    bars: Iterable[PriceBar],
    *,
    split_events: Sequence[SplitEvent],
    as_of: datetime,
    exchange_timezone: str = DEFAULT_EXCHANGE_TIMEZONE,
) -> PriceSeries:
    """The daily series visible at ``as_of`` on the ``as_of`` split basis."""

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    as_of_date = as_of.date()
    events = tuple(event for event in split_events if event.event_date <= as_of_date)
    observations: dict[date, list[PriceBar]] = defaultdict(list)
    for item in bars:
        if item.observed_at <= as_of and item.bar_date <= as_of_date:
            observations[item.bar_date].append(item)

    diagnostics: list[str] = []
    review: list[str] = []
    series: list[AdjustedBar] = []
    for bar_date in sorted(observations):
        ordered = sorted(observations[bar_date], key=lambda item: (item.observed_at, item.id or 0))
        latest = _adjust(ordered[-1], events, as_of_date, exchange_timezone)
        for earlier in ordered[:-1]:
            converted = _adjust(earlier, events, as_of_date, exchange_timezone)
            if abs(converted.close / latest.close - 1) > SUSPECTED_RATIO_TOLERANCE:
                review.append("PRICE_BASIS_CONFLICT")
                diagnostics.append(f"PRICE_BASIS_CONFLICT:{bar_date.isoformat()}")
                break
        series.append(latest)

    used_observations = {item.observed_at for item in series}
    if any(basis_uncertain(events, observation_date(moment, exchange_timezone)) for moment in used_observations):
        review.append("PRICE_BASIS_UNCERTAIN")
    for previous, current in zip(series, series[1:]):
        at_close = suspected_split_ratio(previous.close, current.close)
        if at_close is not None and at_close == suspected_split_ratio(previous.close, current.open):
            review.append("PRICE_SUSPECTED_UNRECORDED_SPLIT")
            diagnostics.append(f"PRICE_SUSPECTED_UNRECORDED_SPLIT:{current.bar_date.isoformat()}")
    currencies = {item.currency for items in observations.values() for item in items}
    if currencies - {SUPPORTED_CURRENCY}:
        review.append("PRICE_CURRENCY_UNSUPPORTED")
        diagnostics.append("PRICE_CURRENCY:" + ",".join(sorted(currencies)))
    return PriceSeries(
        bars=tuple(series),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        review_reasons=tuple(dict.fromkeys(review)),
    )
