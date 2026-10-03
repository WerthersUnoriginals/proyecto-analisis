"""Point-in-time annual fundamentals for A, built on read (sec-annual-v1).

Fiscal years are the real 10-K year ends of the v3 fiscal calendar. Annual
values are resolved per tag by latest visible filing and converted to the
``as_of`` share basis, because a 10-K restates only three years: without the
conversion NVDA's FY2023 EPS YoY reads -95.6% instead of about -55%.

Callers pass SEC facts already limited to ``observed_at <= as_of``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, getcontext
from typing import Iterable, Mapping, Sequence

from database.quarterly_v3 import (
    _Accumulator,
    _resolve_sec_tag_interval,
    build_fiscal_calendar,
    six_years_before,
    verify_provider_events,
)
from database.sec_facts import METRIC_UNITS, SEC_TAG_CATALOG, TAG_TO_METRIC, SecFact, duration_class
from database.split_basis import SplitEvent

ANNUAL_NORMALIZER_VERSION = "sec-annual-v1"
ANNUAL_METRICS = ("EPS_DILUTED", "REVENUE", "NET_INCOME")
ANNUAL_FORMS = frozenset({"10-K", "10-K/A"})
EQUITY_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
INSTANT_METRICS = ("STOCKHOLDERS_EQUITY",)
VALUE_WINDOW_DAYS = round(365.25 * 7)
ROE_PAIRS = (
    ("NetIncomeLoss", "StockholdersEquity"),
    ("ProfitLoss", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
)


@dataclass(frozen=True)
class AnnualValue:
    metric: str
    fiscal_year: int
    concept: str
    period_start: date
    period_end: date
    value: Decimal
    reported_value: Decimal
    basis_factor: Decimal
    filed_date: date
    accession: str
    observed_at: datetime | None
    lineage: tuple[int, ...]


@dataclass(frozen=True)
class EquityValue:
    concept: str
    on: date
    value: Decimal
    filed_date: date
    accession: str
    lineage: tuple[int, ...]


@dataclass(frozen=True)
class AnnualGrowth:
    metric: str
    fiscal_year: int
    status: str
    concept: str | None
    current: AnnualValue | None
    comparable: AnnualValue | None
    yoy_pct: Decimal | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Cagr:
    metric: str
    years: int
    value_pct: Decimal | None
    concept: str | None
    start: AnnualValue | None
    end: AnnualValue | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Roe:
    fiscal_year: int
    status: str
    value_pct: Decimal | None
    concepts: tuple[str, str] | None
    net_income: AnnualValue | None
    opening: EquityValue | None
    closing: EquityValue | None
    reasons: tuple[str, ...] = ()


def _rank(metric: str, concept: str) -> int:
    catalog = SEC_TAG_CATALOG[metric]
    return catalog.index(concept) if concept in catalog else len(catalog)


@dataclass(frozen=True)
class AnnualView:
    as_of: datetime
    candidates: Mapping[str, Mapping[int, tuple[AnnualValue, ...]]]
    equity: Mapping[str, Mapping[date, EquityValue]]
    year_ends: Mapping[int, date]
    diagnostics: tuple[str, ...]
    split_status_reasons: tuple[str, ...]
    rejected_split_events: tuple[SplitEvent, ...] = ()

    def effective(self, metric: str) -> dict[int, AnnualValue]:
        return {year: values[0] for year, values in self.candidates.get(metric, {}).items() if values}

    def latest_year(self, metric: str) -> int | None:
        years = self.effective(metric)
        return max(years) if years else None

    def _value(self, metric: str, year: int, concept: str) -> AnnualValue | None:
        return next((item for item in self.candidates.get(metric, {}).get(year, ()) if item.concept == concept), None)

    def growth(self, metric: str, year: int) -> AnnualGrowth:
        current = self.effective(metric).get(year)
        if current is None:
            return AnnualGrowth(metric, year, "NO_DATA", None, None, None, None, ("NO_VALUE",))
        for candidate in self.candidates[metric][year]:
            comparable = self._value(metric, year - 1, candidate.concept)
            if comparable is None:
                continue
            if comparable.value > 0:
                yoy = (candidate.value / comparable.value - 1) * 100
                return AnnualGrowth(metric, year, "GROWTH", candidate.concept, candidate, comparable, yoy)
            status = "LOSS_TO_PROFIT" if candidate.value > 0 else "LOSS"
            return AnnualGrowth(metric, year, status, candidate.concept, candidate, comparable, None,
                                ("NON_POSITIVE_COMPARABLE",))
        if current.value <= 0:
            return AnnualGrowth(metric, year, "LOSS", current.concept, current, None, None, ("NO_COMPARABLE",))
        return AnnualGrowth(metric, year, "NO_DATA", current.concept, current, None, None, ("NO_COMPARABLE",))

    def cagr(self, metric: str, years: int) -> Cagr:
        latest = self.latest_year(metric)
        if latest is None:
            return Cagr(metric, years, None, None, None, None, ("NO_VALUE",))
        window = range(latest - years, latest + 1)
        by_year = self.candidates.get(metric, {})
        if any(not by_year.get(year) for year in window):
            return Cagr(metric, years, None, None, None, None, ("INSUFFICIENT_HISTORY",))
        concepts = sorted(
            {item.concept for item in by_year[latest]}, key=lambda concept: _rank(metric, concept),
        )
        for concept in concepts:
            series = [self._value(metric, year, concept) for year in window]
            if any(item is None for item in series):
                continue
            start, end = series[0], series[-1]
            if start.value <= 0 or end.value <= 0:
                return Cagr(metric, years, None, concept, start, end, ("NON_POSITIVE_ENDPOINT",))
            getcontext().prec = 28
            ratio = end.value / start.value
            value = (ratio ** (Decimal(1) / Decimal(years)) - 1) * 100
            return Cagr(metric, years, value, concept, start, end)
        return Cagr(metric, years, None, None, None, None, ("NO_COMMON_CONCEPT",))

    def roe(self, year: int) -> Roe:
        closing_date = self.year_ends.get(year)
        opening_date = self.year_ends.get(year - 1)
        incomplete = None
        # Each pair is internally consistent (parent-only or including minority
        # interest); pairs are tried in order and never mixed.
        for income_tag, equity_tag in ROE_PAIRS:
            income = self._value("NET_INCOME", year, income_tag)
            equity = self.equity.get(equity_tag, {})
            if income is None or closing_date is None:
                continue
            closing = equity.get(closing_date)
            opening = equity.get(opening_date) if opening_date else None
            if closing is None or opening is None:
                incomplete = incomplete or Roe(year, "NO_DATA", None, (income_tag, equity_tag), income,
                                               opening, closing, ("EQUITY_NOT_AVAILABLE",))
                continue
            if opening.value <= 0 or closing.value <= 0:
                return Roe(year, "NOT_MEANINGFUL", None, (income_tag, equity_tag), income, opening, closing,
                           ("NON_POSITIVE_EQUITY",))
            average = (opening.value + closing.value) / 2
            return Roe(year, "OK", income.value / average * 100, (income_tag, equity_tag),
                       income, opening, closing)
        return incomplete or Roe(year, "NO_DATA", None, None, None, None, None, ("NET_INCOME_NOT_AVAILABLE",))


def _latest_unique_instant(facts: Sequence[SecFact]) -> SecFact | None:
    latest_filed = max(fact.filed_date for fact in facts)
    latest = [fact for fact in facts if fact.filed_date == latest_filed]
    return latest[0] if len({fact.value for fact in latest}) == 1 else None


def build_annual_view(
    sec_facts: Iterable[SecFact],
    *,
    split_events: Sequence[SplitEvent],
    as_of: datetime,
    window_start: date | None = None,
    metrics: Sequence[str] = ANNUAL_METRICS,
    instant_metrics: Sequence[str] = INSTANT_METRICS,
) -> AnnualView:
    """Build the annual view of one company from evidence visible at ``as_of``.

    ``metrics`` (annual durations) and ``instant_metrics`` (fiscal year-end
    balances, keyed by tag in ``equity``) default to what A needs; S asks for
    share counts and debt without changing A's view.
    """

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    as_of_date = as_of.date()
    window_start = window_start or six_years_before(as_of_date)
    facts = [fact for fact in sec_facts if fact.filed_date <= as_of_date]
    events = tuple(event for event in split_events if event.event_date <= as_of_date)
    calendar = build_fiscal_calendar(facts)
    year_ends = {year.label: year.end for year in calendar.years if not year.projected}
    year_by_end = {end: label for label, end in year_ends.items()}
    value_floor = as_of_date - timedelta(days=VALUE_WINDOW_DAYS)
    acc = _Accumulator()
    events, rejected = verify_provider_events(facts, events, acc)

    grouped: dict[tuple[str, str, tuple[date, date]], list[SecFact]] = defaultdict(list)
    for fact in facts:
        metric = fact.metric
        if (
            metric in metrics
            and fact.form in ANNUAL_FORMS
            and fact.unit == METRIC_UNITS[metric]
            and duration_class(fact) == "ANNUAL"
            and fact.period_end in year_by_end
            and fact.period_end >= value_floor
        ):
            grouped[(metric, fact.tag, (fact.period_start, fact.period_end))].append(fact)

    intervals_per_year: dict[tuple[str, str, int], set] = defaultdict(set)
    for metric, tag, interval in grouped:
        intervals_per_year[(metric, tag, year_by_end[interval[1]])].add(interval)

    candidates: dict[str, dict[int, list[AnnualValue]]] = defaultdict(lambda: defaultdict(list))
    for (metric, tag, interval), group in grouped.items():
        year = year_by_end[interval[1]]
        if len(intervals_per_year[(metric, tag, year)]) > 1:
            acc.diagnostics.append("AMBIGUOUS_ANNUAL_INTERVAL")
            continue
        resolved = _resolve_sec_tag_interval(metric, group, events, as_of_date, acc, window_start)
        if resolved is None:
            continue
        fact, value, factor = resolved
        candidates[metric][year].append(AnnualValue(
            metric=metric, fiscal_year=year, concept=tag, period_start=interval[0], period_end=interval[1],
            value=value, reported_value=fact.value, basis_factor=factor, filed_date=fact.filed_date,
            accession=fact.accession, observed_at=fact.observed_at,
            lineage=tuple(item.id for item in group if item.id is not None),
        ))

    equity_groups: dict[tuple[str, date], list[SecFact]] = defaultdict(list)
    for fact in facts:
        if (
            fact.metric in instant_metrics
            and fact.form in EQUITY_FORMS
            and fact.period_start is None
            and fact.unit == METRIC_UNITS[fact.metric]
            and fact.period_end in year_by_end
            and fact.period_end >= value_floor - timedelta(days=380)
        ):
            equity_groups[(fact.tag, fact.period_end)].append(fact)
    equity: dict[str, dict[date, EquityValue]] = defaultdict(dict)
    for (tag, on), group in equity_groups.items():
        chosen = _latest_unique_instant(group)
        if chosen is None:
            acc.diagnostics.append("AMBIGUOUS_EQUITY" if TAG_TO_METRIC.get(tag) == "STOCKHOLDERS_EQUITY"
                                   else "AMBIGUOUS_INSTANT")
            continue
        equity[tag][on] = EquityValue(
            tag, on, chosen.value, chosen.filed_date, chosen.accession,
            tuple(item.id for item in group if item.id is not None),
        )

    frozen = {
        metric: {
            year: tuple(sorted(values, key=lambda item: _rank(metric, item.concept)))
            for year, values in by_year.items()
        }
        for metric, by_year in candidates.items()
    }
    return AnnualView(
        as_of=as_of,
        candidates=frozen,
        equity={tag: dict(values) for tag, values in equity.items()},
        year_ends=year_ends,
        diagnostics=tuple(dict.fromkeys(acc.diagnostics)),
        split_status_reasons=tuple(dict.fromkeys(acc.split_reasons)),
        rejected_split_events=rejected,
    )
