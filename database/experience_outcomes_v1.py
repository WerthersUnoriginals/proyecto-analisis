"""Forward outcomes and calibration for the Experience Store (spec 2026-10-04-experience-store-v1 §5–§6).

Outcomes are derived on read from the price evidence visible at report time,
never stored. They measure what happened after a snapshot; they are not
probabilities and never change weights.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from database.prices_v3 import AdjustedBar

OUTCOMES_VERSION = "experience-outcomes-v1"
HORIZONS = (21, 63, 126, 252)
STOP_LOSS = Decimal("0.08")
PROFIT_TARGET = Decimal("0.20")
BUCKETS = ((40.0, "<40"), (55.0, "40-55"), (70.0, "55-70"))


def entry_bar(bars: Sequence[AdjustedBar], as_of: datetime) -> AdjustedBar | None:
    eligible = [bar for bar in bars if bar.bar_date <= as_of.date()]
    return max(eligible, key=lambda bar: bar.bar_date) if eligible else None


def _pct(value: Decimal) -> float:
    return round(float(value * 100), 6)


def compute_outcome(stock: Sequence[AdjustedBar], market: Sequence[AdjustedBar], entry_date: date) -> dict:
    stock = sorted(stock, key=lambda bar: bar.bar_date)
    position = next((index for index, bar in enumerate(stock) if bar.bar_date == entry_date), None)
    if position is None:
        return {"status": "NO_ENTRY_PRICE", "horizons": {}, "oneil_rule": None, "version": OUTCOMES_VERSION}
    entry = stock[position]
    market_close = {bar.bar_date: bar.close for bar in market}
    horizons = {}
    for horizon in HORIZONS:
        if position + horizon >= len(stock):
            horizons[horizon] = {"status": "PENDING"}
            continue
        window = stock[position + 1:position + horizon + 1]
        exit_bar = window[-1]
        result = (exit_bar.close / entry.close) - 1
        spy = None
        if entry.bar_date in market_close and exit_bar.bar_date in market_close:
            spy = market_close[exit_bar.bar_date] / market_close[entry.bar_date] - 1
        horizons[horizon] = {
            "status": "MATURED",
            "exit_date": exit_bar.bar_date.isoformat(),
            "return_pct": _pct(result),
            "spy_return_pct": None if spy is None else _pct(spy),
            "excess_pct": None if spy is None else _pct(result - spy),
            "max_drawdown_pct": _pct(min(bar.low for bar in window) / entry.close - 1),
            "max_gain_pct": _pct(max(bar.high for bar in window) / entry.close - 1),
        }
    rule = "PENDING" if position + HORIZONS[-1] >= len(stock) else "NEITHER"
    for bar in stock[position + 1:position + HORIZONS[-1] + 1]:
        if bar.low <= entry.close * (1 - STOP_LOSS):
            rule = "STOP_FIRST"  # a session touching both counts as the stop (conservative)
            break
        if bar.high >= entry.close * (1 + PROFIT_TARGET):
            rule = "TARGET_FIRST"
            break
    return {"status": "OK", "entry_date": entry.bar_date.isoformat(), "entry_close": str(entry.close),
            "horizons": horizons, "oneil_rule": rule, "version": OUTCOMES_VERSION}


def composite_bucket(value) -> str | None:
    if value is None:
        return None
    for limit, name in BUCKETS:
        if float(value) < limit:
            return name
    return ">=70"


def _summary(items: list[tuple[dict, str]]) -> dict:
    returns = [item["return_pct"] for item, _ in items]
    excess = [item["excess_pct"] for item, _ in items if item.get("excess_pct") is not None]
    rules = [rule for _, rule in items]
    count = len(items)
    return {
        "count": count,
        "mean_return_pct": round(sum(returns) / count, 4),
        "mean_excess_pct": round(sum(excess) / len(excess), 4) if excess else None,
        "positive_excess_share": round(sum(1 for value in excess if value > 0) / len(excess), 4) if excess else None,
        "stop_first_share": round(rules.count("STOP_FIRST") / count, 4),
        "target_first_share": round(rules.count("TARGET_FIRST") / count, 4),
    }


def calibration(records: Iterable[Mapping]) -> dict:
    """Matured outcomes grouped by verdict and composite bucket, per horizon."""

    groups: dict[str, dict] = {"by_verdict": defaultdict(lambda: defaultdict(list)),
                               "by_bucket": defaultdict(lambda: defaultdict(list))}
    for record in records:
        outcome = record.get("outcome") or {}
        if outcome.get("status") != "OK":
            continue
        for horizon, item in outcome["horizons"].items():
            if item.get("status") != "MATURED":
                continue
            pair = (item, outcome.get("oneil_rule"))
            groups["by_verdict"][record.get("verdict")][horizon].append(pair)
            bucket = composite_bucket(record.get("composite_score"))
            if bucket:
                groups["by_bucket"][bucket][horizon].append(pair)
    return {
        name: {key: {horizon: _summary(items) for horizon, items in sorted(by_horizon.items())}
               for key, by_horizon in grouped.items()}
        for name, grouped in groups.items()
    }
