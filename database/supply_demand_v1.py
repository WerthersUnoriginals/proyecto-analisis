"""Supply and demand calculations for S (s-supply-demand-v1, spec 2026-10-03 §4).

Share supply and leverage come from the annual view (real fiscal years,
SEC-verified split basis); demand comes from the N price series. Absent
evidence is never read as favorable: no debt tag is not zero debt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from database.annual_v3 import AnnualView
from database.new_highs_v1 import STALE_BUSINESS_DAYS, business_days_between
from database.prices_v3 import AdjustedBar

SUPPLY_DEMAND_VERSION = "s-supply-demand-v1"
S_ANNUAL_METRICS = ("DILUTED_SHARES", "BASIC_SHARES")
S_INSTANT_METRICS = ("STOCKHOLDERS_EQUITY", "DEBT")
SHARE_CHANGE_YEARS = 3
DEBT_TREND_YEARS = 3
VOLUME_SESSIONS = 50
# Tried in order and never mixed: a total, or a noncurrent amount with its own
# current part. A noncurrent amount alone would understate debt.
DEBT_DEFINITIONS: tuple[tuple[str, ...], ...] = (
    ("LongTermDebt",),
    ("LongTermDebtNoncurrent", "LongTermDebtCurrent"),
    ("LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
    ("DebtLongtermAndShorttermCombinedAmount",),
)
EQUITY_CONCEPTS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)


@dataclass(frozen=True)
class ShareSupply:
    latest_fiscal_year: int | None
    diluted_shares_latest: Decimal | None
    change_1y_pct: Decimal | None
    change_3y_pct: Decimal | None
    concept: str | None
    reasons_1y: tuple[str, ...] = ()
    reasons_3y: tuple[str, ...] = ()
    lineage: tuple[int, ...] = ()


@dataclass(frozen=True)
class Leverage:
    status: str
    fiscal_year: int | None = None
    definition: str | None = None
    equity_concept: str | None = None
    debt_latest: Decimal | None = None
    equity_latest: Decimal | None = None
    debt_to_equity_latest: Decimal | None = None
    debt_to_equity_3y_ago: Decimal | None = None
    change_3y: Decimal | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class VolumeDemand:
    status: str
    up_down_volume_ratio_50d: Decimal | None = None
    sessions: int = 0
    up_volume: Decimal | None = None
    down_volume: Decimal | None = None
    reasons: tuple[str, ...] = ()


def _share_metric(view: AnnualView) -> str | None:
    """Diluted shares, or basic shares when they are strictly more recent."""

    diluted, basic = view.latest_year("DILUTED_SHARES"), view.latest_year("BASIC_SHARES")
    if diluted is None and basic is None:
        return None
    if basic is not None and (diluted is None or basic > diluted):
        return "BASIC_SHARES"
    return "DILUTED_SHARES"


def compute_share_supply(view: AnnualView) -> ShareSupply:
    metric = _share_metric(view)
    if metric is None:
        return ShareSupply(None, None, None, None, None, ("NO_VALUE",), ("NO_VALUE",))
    latest = view.latest_year(metric)
    current = view.effective(metric)[latest]
    growth = view.growth(metric, latest)
    window = view.cagr(metric, SHARE_CHANGE_YEARS)
    change_3y = None
    if window.start is not None and window.end is not None and window.value_pct is not None:
        change_3y = (window.end.value / window.start.value - 1) * 100
    lineage = current.lineage + (window.start.lineage if window.start else ())
    return ShareSupply(
        latest_fiscal_year=latest,
        diluted_shares_latest=current.value,
        change_1y_pct=growth.yoy_pct,
        change_3y_pct=change_3y,
        concept=current.concept,
        reasons_1y=growth.reasons,
        reasons_3y=window.reasons,
        lineage=lineage,
    )


def _debt(view: AnnualView, definition: tuple[str, ...], on: date) -> Decimal | None:
    parts = [view.equity.get(tag, {}).get(on) for tag in definition]
    if any(part is None for part in parts):
        return None
    return sum((part.value for part in parts), Decimal(0))


def _equity(view: AnnualView, on: date) -> tuple[str, Decimal] | None:
    for concept in EQUITY_CONCEPTS:
        value = view.equity.get(concept, {}).get(on)
        if value is not None:
            return concept, value.value
    return None


def compute_leverage(view: AnnualView) -> Leverage:
    years = sorted(view.year_ends, reverse=True)
    for year in years:
        on = view.year_ends[year]
        equity = _equity(view, on)
        definition = next((item for item in DEBT_DEFINITIONS if _debt(view, item, on) is not None), None)
        if equity is None or definition is None:
            continue
        equity_concept, equity_value = equity
        debt = _debt(view, definition, on)
        name = "+".join(definition)
        if equity_value <= 0:
            return Leverage("NOT_MEANINGFUL", year, name, equity_concept, debt, equity_value,
                            reasons=("NON_POSITIVE_EQUITY",))
        ratio = debt / equity_value
        previous_on = view.year_ends.get(year - DEBT_TREND_YEARS)
        previous_ratio, reasons = None, ()
        if previous_on is not None:
            previous_debt = _debt(view, definition, previous_on)
            previous_equity = view.equity.get(equity_concept, {}).get(previous_on)
            if previous_debt is None or previous_equity is None:
                reasons = ("NO_COMPARABLE_DEBT_3Y",)
            elif previous_equity.value <= 0:
                reasons = ("NON_POSITIVE_EQUITY_3Y",)
            else:
                previous_ratio = previous_debt / previous_equity.value
        else:
            reasons = ("NO_COMPARABLE_DEBT_3Y",)
        return Leverage(
            "OK", year, name, equity_concept, debt, equity_value, ratio, previous_ratio,
            None if previous_ratio is None else ratio - previous_ratio, reasons,
        )
    return Leverage("NO_DEBT_EVIDENCE", reasons=("NO_DEBT_EVIDENCE",))


def compute_volume_demand(bars: Sequence[AdjustedBar], as_of_date: date) -> VolumeDemand:
    bars = sorted((item for item in bars if item.bar_date <= as_of_date), key=lambda item: item.bar_date)
    if len(bars) < VOLUME_SESSIONS + 1:
        return VolumeDemand("INSUFFICIENT_HISTORY", sessions=max(len(bars) - 1, 0),
                            reasons=("INSUFFICIENT_PRICE_HISTORY",))
    window = bars[-(VOLUME_SESSIONS + 1):]
    up = sum((current.volume for previous, current in zip(window, window[1:]) if current.close > previous.close),
             Decimal(0))
    down = sum((current.volume for previous, current in zip(window, window[1:]) if current.close < previous.close),
               Decimal(0))
    status = "STALE_PRICES" if business_days_between(bars[-1].bar_date, as_of_date) > STALE_BUSINESS_DAYS else "OK"
    if down == 0:
        return VolumeDemand(status, None, VOLUME_SESSIONS, up, down, ("NO_DOWN_VOLUME",))
    return VolumeDemand(status, up / down, VOLUME_SESSIONS, up, down)
