"""Point-in-time quarterly fundamentals built on read from raw evidence (v3).

Pipeline for one company at ``as_of``:

1. the fiscal calendar comes from each filing's *own* reporting period, never
   from a comparative's metadata;
2. SEC values are converted to the share basis in force at ``as_of``
   (split-basis-v1) and resolved by latest filing per tag and quarter;
3. Q4 revenue and net income are derived as FY minus 9M YTD when SEC does not
   report Q4;
4. Yahoo values fill quarters without SEC evidence;
5. growth is computed within one source and one concept (tag).

Callers pass SEC facts and Yahoo rows already limited to
``observed_at <= as_of``. Independently, nothing filed after ``as_of`` is used.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from database.sec_facts import (
    METRIC_UNITS,
    PERIODIC_FORMS,
    SEC_TAG_CATALOG,
    SecFact,
    duration_class,
)
from database.split_basis import (
    SplitEvent,
    basis_factor,
    suspected_split_ratio,
)

QUARTERLY_NORMALIZER_VERSION = "sec-quarterly-v3"
Q4_DERIVATION_VERSION = "q4-derivation-v1"
SELECTION_POLICY_VERSION = "c-quarterly-v3"

QUARTER_METRICS = ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")
PER_SHARE_METRICS = frozenset({"EPS_DILUTED"})
SHARE_COUNT_METRICS = frozenset({"DILUTED_SHARES"})
DERIVABLE_Q4_METRICS = frozenset({"REVENUE", "NET_INCOME"})
CALENDAR_METRICS = frozenset({"EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES"})

YAHOO_ALIGNMENT_DAYS = 35
PROJECTION_TOLERANCE_DAYS = 10
QUARTER_DAYS = 91.3125
FISCAL_YEAR_MIN_DAYS = 350
FISCAL_YEAR_MAX_DAYS = 380
PER_SHARE_PAIR_TOLERANCE = Decimal("0.0101")
RELATIVE_PAIR_TOLERANCE = Decimal("0.005")
Q4_MISMATCH_TOLERANCE = Decimal("0.005")

# Yahoo provider names grouped into comparable concepts. Different datasets that
# publish the same GAAP line share a concept; different lines never do.
YAHOO_CONCEPTS: Mapping[str, tuple[str, str]] = {
    "quarterlyDilutedEPS": ("EPS_DILUTED", "yahoo:DilutedEPS"),
    "Diluted EPS": ("EPS_DILUTED", "yahoo:DilutedEPS"),
    "DilutedEPS": ("EPS_DILUTED", "yahoo:DilutedEPS"),
    "quarterlyTotalRevenue": ("REVENUE", "yahoo:TotalRevenue"),
    "Total Revenue": ("REVENUE", "yahoo:TotalRevenue"),
    "Operating Revenue": ("REVENUE", "yahoo:OperatingRevenue"),
    "quarterlyNetIncome": ("NET_INCOME", "yahoo:NetIncome"),
    "Net Income": ("NET_INCOME", "yahoo:NetIncome"),
    "Net Income Common Stockholders": ("NET_INCOME", "yahoo:NetIncomeCommon"),
}
YAHOO_VARIANT_PRIORITY = ("yahoo.fundamentals_timeseries", "yfinance.quarterly_income_stmt")


@dataclass(frozen=True, order=True)
class QuarterKey:
    fiscal_year: int
    fiscal_quarter: int

    @property
    def index(self) -> int:
        return self.fiscal_year * 4 + self.fiscal_quarter - 1

    @classmethod
    def from_index(cls, index: int) -> "QuarterKey":
        return cls(index // 4, index % 4 + 1)

    def shift(self, quarters: int) -> "QuarterKey":
        return QuarterKey.from_index(self.index + quarters)


Interval = tuple[date, date]


@dataclass(frozen=True)
class QuarterIdentity:
    key: QuarterKey | None
    period_start: date
    period_end: date
    method: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class FiscalYear:
    label: int
    start: date
    end: date
    projected: bool = False


@dataclass(frozen=True)
class FiscalCalendar:
    quarters: Mapping[Interval, QuarterIdentity]
    annuals: Mapping[Interval, int]
    years: tuple[FiscalYear, ...] = ()
    reasons: tuple[str, ...] = ()

    def anchors(self) -> list[tuple[date, QuarterKey]]:
        result = [
            (identity.period_end, identity.key)
            for identity in self.quarters.values() if identity.key is not None
        ]
        result += [(end, QuarterKey(fy, 4)) for (_, end), fy in self.annuals.items()]
        return result

    def key_for_end(self, end: date) -> QuarterKey | None:
        """Locate a quarter end by its position inside the fiscal year."""

        matches = set()
        for year in _extended_years(self.years):
            position = (end - year.start).days / QUARTER_DAYS
            if 0.5 < position <= 4.5:
                matches.add(QuarterKey(year.label, min(4, max(1, round(position)))))
        return next(iter(matches)) if len(matches) == 1 else None


@dataclass(frozen=True)
class QuarterValue:
    metric: str
    key: QuarterKey
    source: str
    source_variant: str
    concept: str
    kind: str
    period_start: date | None
    period_end: date
    value: Decimal
    reported_value: Decimal
    basis_factor: Decimal
    filed_date: date | None
    accession: str | None
    observed_at: datetime | None
    lineage: tuple[int, ...]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Growth:
    metric: str
    key: QuarterKey
    source: str | None
    concept: str | None
    current: QuarterValue | None
    comparable: QuarterValue | None
    yoy_pct: Decimal | None
    loss_to_profit: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceComparison:
    metric: str
    key: QuarterKey
    sec: QuarterValue
    yahoo: QuarterValue
    difference_pct: Decimal


# --------------------------------------------------------------------------
# Fiscal calendar
# --------------------------------------------------------------------------

def _interval(fact: SecFact) -> Interval:
    return fact.period_start, fact.period_end


def _quarter_number(fiscal_period: str | None) -> int | None:
    return int(fiscal_period[1]) if fiscal_period in {"Q1", "Q2", "Q3"} else None


def _own_claims(periodic: Sequence[SecFact]) -> tuple[dict[Interval, set[QuarterKey]], list[tuple[date, date, int]]]:
    """Fiscal claims a filing makes about its *own* period only."""

    by_accession: dict[str, list[SecFact]] = defaultdict(list)
    for fact in periodic:
        by_accession[fact.accession].append(fact)
    quarter_claims: dict[Interval, set[QuarterKey]] = defaultdict(set)
    annual_claims: list[tuple[date, date, int]] = []  # (filed, annual end, fy)
    for group in by_accession.values():
        form = group[0].form
        annual = [fact for fact in group if duration_class(fact) == "ANNUAL"]
        quarters = [fact for fact in group if duration_class(fact) == "QUARTER"]
        if form.startswith("10-K") and annual:
            own_end = max(fact.period_end for fact in annual)
            for fact in annual:
                if fact.period_end == own_end and fact.fiscal_period == "FY" and fact.fiscal_year:
                    annual_claims.append((fact.filed_date, own_end, fact.fiscal_year))
        elif form.startswith("10-Q") and quarters:
            own_end = max(fact.period_end for fact in quarters)
            for fact in quarters:
                number = _quarter_number(fact.fiscal_period)
                if fact.period_end == own_end and number and fact.fiscal_year is not None:
                    quarter_claims[_interval(fact)].add(QuarterKey(fact.fiscal_year, number))
    return quarter_claims, annual_claims


def _fiscal_years(periodic: Sequence[SecFact], annual_claims, reasons: list[str]) -> tuple[FiscalYear, ...]:
    """Fiscal years from real 10-K year-end dates; labels use one offset."""

    intervals = {
        _interval(fact) for fact in periodic
        if fact.form.startswith("10-K")
        and FISCAL_YEAR_MIN_DAYS <= (fact.period_end - fact.period_start).days <= FISCAL_YEAR_MAX_DAYS
    }
    if not intervals:
        return ()
    starts_by_end: dict[date, date] = {}
    for start, end in intervals:
        starts_by_end[end] = min(start, starts_by_end.get(end, start))
    ends = sorted(starts_by_end)
    spans: list[tuple[date, date, bool]] = []
    for index, end in enumerate(ends):
        if index == 0:
            spans.append((starts_by_end[end], end, False))
            continue
        previous = ends[index - 1]
        gap = (end - previous).days
        if gap < FISCAL_YEAR_MIN_DAYS:
            reasons.append("FISCAL_YEAR_END_CHANGE")
            spans.append((previous + timedelta(days=1), end, True))
            continue
        missing = round(gap / 365.25) - 1
        for step in range(missing):
            projected_end = previous + timedelta(days=round(gap * (step + 1) / (missing + 1)))
            spans.append((spans[-1][1] + timedelta(days=1), projected_end, True))
        spans.append((spans[-1][1] + timedelta(days=1), end, False))

    def raw_label(start: date, end: date) -> int:
        return (start + (end - start) / 2).year

    offset = 0
    if annual_claims:
        _, claimed_end, claimed_year = max(annual_claims)
        for start, end, _ in spans:
            if end == claimed_end:
                offset = claimed_year - raw_label(start, end)
    return tuple(
        FiscalYear(raw_label(start, end) + offset, start, end, projected)
        for start, end, projected in spans
    )


def _extended_years(years: Sequence[FiscalYear]) -> list[FiscalYear]:
    if not years:
        return []
    result = list(years)
    lengths = [(year.end - year.start).days + 1 for year in years if not year.projected] or [365]
    length = round(sum(lengths) / len(lengths))
    last = result[-1]
    for _ in range(2):
        last = FiscalYear(last.label + 1, last.end + timedelta(days=1), last.end + timedelta(days=length), True)
        result.append(last)
    first = result[0]
    for _ in range(2):
        first = FiscalYear(first.label - 1, first.start - timedelta(days=length), first.start - timedelta(days=1), True)
        result.insert(0, first)
    return result


def build_fiscal_calendar(facts: Iterable[SecFact]) -> FiscalCalendar:
    """Identify fiscal years from real year-end dates and quarters by position.

    The ``fy`` label SEC attaches to a fact is the *filing's* focus and is not
    reliable across years (NVDA labels fiscal 2012 quarters as 2011), so it is
    never used to place a period. Own-period ``fp`` claims only corroborate.
    """

    periodic = [
        fact for fact in facts
        if fact.form in PERIODIC_FORMS
        and fact.period_start is not None
        and fact.metric in CALENDAR_METRICS
    ]
    reasons: list[str] = []
    quarter_claims, annual_claims = _own_claims(periodic)
    years = _fiscal_years(periodic, annual_claims, reasons)
    locator = FiscalCalendar({}, {}, years)

    annuals = {}
    for fact in periodic:
        interval = _interval(fact)
        if duration_class(fact) == "ANNUAL":
            for year in years:
                if year.end == interval[1] and not year.projected:
                    annuals[interval] = year.label

    quarters: dict[Interval, QuarterIdentity] = {}
    quarter_intervals = {_interval(fact) for fact in periodic if duration_class(fact) == "QUARTER"}
    for interval in quarter_intervals:
        claims = quarter_claims.get(interval, set())
        if years:
            key = locator.key_for_end(interval[1])
            short_year = any(
                year.start <= interval[1] <= year.end
                and (year.end - year.start).days < FISCAL_YEAR_MIN_DAYS for year in years
            )
            if key is None or short_year:
                quarters[interval] = QuarterIdentity(None, *interval, "UNRESOLVED", ("FISCAL_IDENTITY_UNRESOLVED",))
            elif claims and {claim.fiscal_quarter for claim in claims} != {key.fiscal_quarter}:
                quarters[interval] = QuarterIdentity(None, *interval, "UNRESOLVED", ("FISCAL_PERIOD_CONTRADICTION",))
            else:
                quarters[interval] = QuarterIdentity(key, *interval, "YEAR_POSITION")
        elif len(claims) == 1:
            quarters[interval] = QuarterIdentity(next(iter(claims)), *interval, "OWN_PERIOD")
        elif len(claims) > 1:
            quarters[interval] = QuarterIdentity(None, *interval, "UNRESOLVED", ("CONTRADICTORY_FISCAL_QUARTER",))

    if not years:
        anchored = [(identity.period_end, identity.key) for identity in quarters.values() if identity.key]
        for interval in quarter_intervals - set(quarters):
            projected = _project(interval[1], anchored)
            if projected is not None:
                quarters[interval] = QuarterIdentity(projected, *interval, "PROJECTION")
            else:
                quarters[interval] = QuarterIdentity(None, *interval, "UNRESOLVED", ("FISCAL_IDENTITY_UNRESOLVED",))

    by_key: dict[QuarterKey, set[Interval]] = defaultdict(set)
    for interval, identity in quarters.items():
        if identity.key is not None:
            by_key[identity.key].add(interval)
    for key, intervals in by_key.items():
        if len(intervals) > 1:
            for interval in intervals:
                quarters[interval] = QuarterIdentity(None, *interval, "UNRESOLVED", ("DUPLICATE_FISCAL_QUARTER",))
    return FiscalCalendar(quarters, annuals, years, tuple(dict.fromkeys(reasons)))


def _project(end: date, anchors: Sequence[tuple[date, QuarterKey]]) -> QuarterKey | None:
    """Locate a date by whole fiscal years from known quarter ends."""

    keys = set()
    for anchor_end, key in anchors:
        for years in range(-8, 9):
            if years == 0:
                continue
            target = anchor_end + timedelta(days=round(365.25 * years))
            if abs((end - target).days) <= PROJECTION_TOLERANCE_DAYS:
                keys.add(key.shift(4 * years))
    return next(iter(keys)) if len(keys) == 1 else None


def _yahoo_key(end: date, calendar: FiscalCalendar) -> QuarterKey | None:
    if calendar.years:
        return calendar.key_for_end(end)
    anchors = calendar.anchors()
    candidates = [(abs((end - anchor_end).days), key) for anchor_end, key in anchors]
    candidates += [
        (abs((end - (anchor_end + timedelta(days=round(QUARTER_DAYS * quarters)))).days), key.shift(quarters))
        for anchor_end, key in anchors for quarters in (1, 2, 3, 4, 8)
    ]
    candidates = [item for item in candidates if item[0] <= YAHOO_ALIGNMENT_DAYS]
    if not candidates:
        return None
    nearest = min(distance for distance, _ in candidates)
    keys = {key for distance, key in candidates if distance == nearest}
    return next(iter(keys)) if len(keys) == 1 else None


# --------------------------------------------------------------------------
# SEC values
# --------------------------------------------------------------------------

def _adjust(metric: str, value: Decimal, factor: Decimal) -> Decimal:
    if metric in PER_SHARE_METRICS:
        return value / factor
    if metric in SHARE_COUNT_METRICS:
        return value * factor
    return value


def _agree(metric: str, first: Decimal, second: Decimal) -> bool:
    difference = abs(first - second)
    if metric in PER_SHARE_METRICS and difference <= PER_SHARE_PAIR_TOLERANCE:
        return True
    scale = max(abs(first), abs(second))
    return scale == 0 or difference / scale <= RELATIVE_PAIR_TOLERANCE


def _factor_hypotheses(events: Sequence[SplitEvent], filed: date, as_of: date) -> list[Decimal]:
    near = [event for event in events if abs((filed - event.event_date).days) <= 7 and event.event_date <= as_of]
    if not near:
        return [basis_factor(events, filed, as_of)]
    far = [event for event in events if event not in near]
    base = basis_factor(far, filed, as_of)
    with_near = base
    for event in near:
        with_near *= event.ratio
    return [with_near, base]


@dataclass
class _Accumulator:
    diagnostics: list[str] = field(default_factory=list)
    split_reasons: list[str] = field(default_factory=list)


def _resolve_sec_tag_interval(
    metric: str,
    facts: Sequence[SecFact],
    events: Sequence[SplitEvent],
    as_of: date,
    acc: _Accumulator,
    window_start: date,
) -> tuple[SecFact, Decimal, Decimal] | None:
    """Return (fact, adjusted value, factor) for one tag and interval."""

    certain: list[tuple[SecFact, Decimal, Decimal]] = []
    uncertain: list[SecFact] = []
    for fact in facts:
        if metric in PER_SHARE_METRICS | SHARE_COUNT_METRICS:
            hypotheses = _factor_hypotheses(events, fact.filed_date, as_of)
        else:
            hypotheses = [Decimal(1)]
        if len(hypotheses) == 1:
            certain.append((fact, _adjust(metric, fact.value, hypotheses[0]), hypotheses[0]))
        else:
            uncertain.append(fact)
    for fact in uncertain:
        options = _factor_hypotheses(events, fact.filed_date, as_of)
        matching = {
            factor for factor in options
            if any(_agree(metric, _adjust(metric, fact.value, factor), value) for _, value, _ in certain)
        }
        if len(matching) == 1:
            factor = next(iter(matching))
            certain.append((fact, _adjust(metric, fact.value, factor), factor))
        else:
            acc.diagnostics.append("SPLIT_BASIS_UNCERTAIN")
    if not certain:
        return None

    certain.sort(key=lambda item: (item[0].filed_date, item[0].accession))
    if metric in PER_SHARE_METRICS | SHARE_COUNT_METRICS:
        for (first, first_value, _), (second, second_value, _) in zip(certain, certain[1:]):
            if (
                first.accession == second.accession
                or first.filed_date < window_start
                or _agree(metric, first_value, second_value)
            ):
                continue
            if suspected_split_ratio(first_value, second_value) is not None:
                acc.split_reasons.append("UNDECLARED_SPLIT_SUSPECTED")
            else:
                acc.diagnostics.append("RESTATED_VALUE")
    latest_filed = certain[-1][0].filed_date
    latest = [item for item in certain if item[0].filed_date == latest_filed]
    if len({value for _, value, _ in latest}) > 1:
        acc.diagnostics.append("AMBIGUOUS_LATEST_FILING")
        return None
    return latest[-1]


SHARES_RATIO_TOLERANCE = Decimal("0.01")
EPS_RATIO_TOLERANCE = Decimal("0.03")


def _pair_verdicts(facts: Sequence[SecFact], event: SplitEvent, metrics, tolerance: Decimal) -> set[str]:
    """Compare values published before and after ``event`` for the same interval."""

    by_interval: dict[tuple[str, Interval], list[SecFact]] = defaultdict(list)
    for fact in facts:
        if fact.metric in metrics and duration_class(fact) == "QUARTER":
            by_interval[(fact.tag, _interval(fact))].append(fact)
    verdicts = set()
    for group in by_interval.values():
        before = [fact for fact in group if fact.filed_date < event.event_date - timedelta(days=7)]
        after = [fact for fact in group if fact.filed_date > event.event_date + timedelta(days=7)]
        if not before or not after:
            continue
        first = max(before, key=lambda fact: fact.filed_date)
        second = min(after, key=lambda fact: fact.filed_date)
        if first.value <= 0 or second.value <= 0:
            continue
        # Shares grow by the ratio after a split; per-share values shrink by it.
        if first.metric in SHARE_COUNT_METRICS:
            observed = second.value / first.value
        else:
            observed = first.value / second.value
        if abs(observed / event.ratio - 1) <= tolerance:
            verdicts.add("VERIFIED")
        elif abs(observed - 1) <= tolerance:
            verdicts.add("CONTRADICTED")
    return verdicts


def verify_provider_events(
    facts: Sequence[SecFact], events: Sequence[SplitEvent], acc: "_Accumulator",
) -> tuple[tuple[SplitEvent, ...], tuple[SplitEvent, ...]]:
    """Split provider-only events into (applied, rejected) using SEC evidence.

    Share counts are published to the unit, so they discriminate even small
    ratios; EPS is rounded to cents and is used only when shares are silent.
    SEC is the authority: a provider event that SEC contradicts (no change in
    restated values) is rejected and never applied, e.g. a spin-off price
    adjustment that yfinance lists as a 1.032 "split".
    """

    applied, rejected = [], []
    for event in events:
        if event.sources != ("YAHOO",):
            applied.append(event)
            continue
        verdicts = _pair_verdicts(facts, event, SHARE_COUNT_METRICS, SHARES_RATIO_TOLERANCE)
        if not verdicts:
            verdicts = _pair_verdicts(facts, event, PER_SHARE_METRICS, EPS_RATIO_TOLERANCE)
        if verdicts == {"CONTRADICTED"}:
            rejected.append(event)
            acc.diagnostics.append("PROVIDER_SPLIT_REJECTED_BY_SEC")
            continue
        if "CONTRADICTED" in verdicts:
            acc.split_reasons.append("PROVIDER_SPLIT_CONTRADICTORY_EVIDENCE")
        elif "VERIFIED" in verdicts:
            acc.diagnostics.append("PROVIDER_SPLIT_VERIFIED_BY_PAIRS")
        else:
            acc.diagnostics.append("PROVIDER_SPLIT_NOT_YET_VERIFIABLE")
        applied.append(event)
    return tuple(applied), tuple(rejected)


def _sec_quarter_values(
    facts: Sequence[SecFact],
    calendar: FiscalCalendar,
    events: Sequence[SplitEvent],
    as_of: date,
    acc: _Accumulator,
    window_start: date,
) -> list[QuarterValue]:
    grouped: dict[tuple[str, str, Interval], list[SecFact]] = defaultdict(list)
    for fact in facts:
        metric = fact.metric
        if (
            metric in QUARTER_METRICS
            and fact.form in PERIODIC_FORMS
            and fact.unit == METRIC_UNITS[metric]
            and duration_class(fact) == "QUARTER"
        ):
            grouped[(metric, fact.tag, _interval(fact))].append(fact)
    values = []
    for (metric, tag, interval), group in grouped.items():
        identity = calendar.quarters.get(interval)
        if identity is None or identity.key is None:
            acc.diagnostics.extend(identity.reasons if identity else ("FISCAL_IDENTITY_UNRESOLVED",))
            continue
        resolved = _resolve_sec_tag_interval(metric, group, events, as_of, acc, window_start)
        if resolved is None:
            continue
        fact, value, factor = resolved
        values.append(QuarterValue(
            metric=metric, key=identity.key, source="SEC", source_variant="sec.company_facts",
            concept=tag, kind="REPORTED", period_start=interval[0], period_end=interval[1],
            value=value, reported_value=fact.value, basis_factor=factor,
            filed_date=fact.filed_date, accession=fact.accession, observed_at=fact.observed_at,
            lineage=tuple(item.id for item in group if item.id is not None),
        ))
    return values


def _latest_unique(facts: Sequence[SecFact]) -> SecFact | None:
    if not facts:
        return None
    latest_filed = max(fact.filed_date for fact in facts)
    latest = [fact for fact in facts if fact.filed_date == latest_filed]
    return latest[0] if len({fact.value for fact in latest}) == 1 else None


def _derived_q4_values(
    facts: Sequence[SecFact],
    calendar: FiscalCalendar,
    reported: Sequence[QuarterValue],
    acc: _Accumulator,
) -> list[QuarterValue]:
    by_tag_interval: dict[tuple[str, str, Interval], list[SecFact]] = defaultdict(list)
    for fact in facts:
        if (
            fact.metric in DERIVABLE_Q4_METRICS
            and fact.form in PERIODIC_FORMS
            and fact.unit == METRIC_UNITS[fact.metric]
            and duration_class(fact) in {"ANNUAL", "YTD_9M"}
        ):
            by_tag_interval[(fact.metric, fact.tag, _interval(fact))].append(fact)
    reported_by = {(item.metric, item.concept, item.key): item for item in reported}
    derived = []
    for (metric, tag, annual), annual_facts in by_tag_interval.items():
        year = calendar.annuals.get(annual)
        if year is None or (annual[1] - annual[0]).days < 330:
            continue
        nine_months = [
            (interval, group) for (m, t, interval), group in by_tag_interval.items()
            if m == metric and t == tag and interval[0] == annual[0]
            and 70 <= (annual[1] - interval[1]).days <= 110
            and 250 <= (interval[1] - interval[0]).days <= 290
        ]
        if len(nine_months) != 1:
            continue
        nine_interval, nine_facts = nine_months[0]
        annual_fact = _latest_unique(annual_facts)
        nine_fact = _latest_unique(nine_facts)
        if annual_fact is None or nine_fact is None:
            acc.diagnostics.append("Q4_DERIVATION_AMBIGUOUS")
            continue
        key = QuarterKey(year, 4)
        value = annual_fact.value - nine_fact.value
        existing = reported_by.get((metric, tag, key))
        if existing is not None:
            scale = max(abs(existing.value), abs(value))
            if scale and abs(existing.value - value) / scale > Q4_MISMATCH_TOLERANCE:
                acc.diagnostics.append("Q4_DERIVATION_MISMATCH")
            continue
        observed = [item.observed_at for item in (annual_fact, nine_fact) if item.observed_at]
        derived.append(QuarterValue(
            metric=metric, key=key, source="SEC", source_variant="derived.sec_fy_minus_9m",
            concept=tag, kind="DERIVED", period_start=nine_interval[1] + timedelta(days=1),
            period_end=annual[1], value=value, reported_value=value, basis_factor=Decimal(1),
            filed_date=max(annual_fact.filed_date, nine_fact.filed_date),
            accession=annual_fact.accession,
            observed_at=max(observed) if observed else None,
            lineage=tuple(item.id for item in (annual_fact, nine_fact) if item.id is not None),
            reasons=(Q4_DERIVATION_VERSION,),
        ))
    return derived


# --------------------------------------------------------------------------
# Yahoo values
# --------------------------------------------------------------------------

def yahoo_rows_from_snapshot(rows: Iterable[Mapping], *, as_of: datetime) -> list[dict]:
    """Keep, per Yahoo dataset, only the rows returned by its latest visible run.

    Each input row carries ``run_observed_at``: one row per (run, raw row).
    """

    latest: dict[str, datetime] = {}
    rows = list(rows)
    for row in rows:
        observed = row["run_observed_at"]
        if observed <= as_of:
            variant = row["source_variant"]
            latest[variant] = max(latest.get(variant, observed), observed)
    selected = []
    for row in rows:
        if latest.get(row["source_variant"]) == row["run_observed_at"]:
            selected.append({**row, "observed_at": row["run_observed_at"]})
    return sorted(selected, key=lambda row: (row["source_variant"], row["period_end"], row["id"]))


def _yahoo_quarter_values(
    rows: Sequence[Mapping],
    sec_facts: Sequence[SecFact],
    events: Sequence[SplitEvent],
    as_of: datetime,
    acc: _Accumulator,
) -> list[QuarterValue]:
    calendars: dict[datetime | None, FiscalCalendar] = {}
    candidates: dict[tuple[str, str, QuarterKey], dict[str, list[QuarterValue]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        name = row.get("source_metric_name")
        mapping = YAHOO_CONCEPTS.get(name)
        if mapping is None or mapping[0] != row.get("metric"):
            continue
        metric, concept = mapping
        observed = row.get("observed_at")
        if observed not in calendars:
            # Identity uses only SEC evidence visible when Yahoo was observed.
            calendars[observed] = build_fiscal_calendar([
                fact for fact in sec_facts
                if observed is None or fact.observed_at is None or fact.observed_at <= observed
            ])
        key = _yahoo_key(row["period_end"], calendars[observed])
        if key is None:
            acc.diagnostics.append("YAHOO_FISCAL_IDENTITY_UNRESOLVED")
            continue
        basis_date = observed.date() if observed else as_of.date()
        factor = basis_factor(events, basis_date, as_of.date()) if metric in PER_SHARE_METRICS else Decimal(1)
        value = Decimal(str(row["value"]))
        candidates[(metric, concept, key)][row["source_variant"]].append(QuarterValue(
            metric=metric, key=key, source="YAHOO", source_variant=row["source_variant"],
            concept=concept, kind="REPORTED", period_start=None, period_end=row["period_end"],
            value=_adjust(metric, value, factor), reported_value=value, basis_factor=factor,
            filed_date=None, accession=None, observed_at=observed, lineage=(row["id"],),
        ))
    values = []
    for (metric, concept, key), by_variant in candidates.items():
        for variant in YAHOO_VARIANT_PRIORITY:
            items = by_variant.get(variant, [])
            if not items:
                continue
            if len({item.value for item in items}) > 1:
                acc.diagnostics.append("YAHOO_AMBIGUOUS_QUARTER")
                break
            values.append(items[0])
            break
    return values


def six_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 6)
    except ValueError:
        return day.replace(year=day.year - 6, day=28)


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------

def _priority(item: QuarterValue) -> tuple:
    if item.source == "SEC":
        catalog = SEC_TAG_CATALOG[item.metric]
        rank = catalog.index(item.concept) if item.concept in catalog else len(catalog)
        # Concept first (the right line item), then reported before derived.
        return (0, rank, 0 if item.kind == "REPORTED" else 1)
    return (2, 0 if item.concept.endswith(("DilutedEPS", "TotalRevenue", "NetIncome")) else 1)


@dataclass(frozen=True)
class QuarterlyView:
    as_of: datetime
    candidates: Mapping[str, Mapping[QuarterKey, tuple[QuarterValue, ...]]]
    calendar: FiscalCalendar
    diagnostics: tuple[str, ...]
    split_status_reasons: tuple[str, ...]
    rejected_split_events: tuple[SplitEvent, ...] = ()

    def effective(self, metric: str) -> dict[QuarterKey, QuarterValue]:
        return {
            key: min(values, key=_priority)
            for key, values in self.candidates.get(metric, {}).items() if values
        }

    def growth(self, metric: str, key: QuarterKey) -> Growth:
        current = self.effective(metric).get(key)
        if current is None:
            return Growth(metric, key, None, None, None, None, None, False, ("NO_VALUE",))
        by_key = self.candidates.get(metric, {})
        prior_key = key.shift(-4)
        same_source_now = sorted(
            (item for item in by_key.get(key, ()) if item.source == current.source), key=_priority,
        )
        prior_values = [item for item in by_key.get(prior_key, ()) if item.source == current.source]
        for candidate in same_source_now:
            comparables = sorted(
                (item for item in prior_values if item.concept == candidate.concept), key=_priority,
            )
            if not comparables:
                continue
            comparable = comparables[0]
            loss_to_profit = comparable.value <= 0 < candidate.value
            if comparable.value <= 0:
                return Growth(metric, key, current.source, candidate.concept, candidate, comparable,
                              None, loss_to_profit, ("NON_POSITIVE_COMPARABLE",))
            yoy = (candidate.value / comparable.value - 1) * 100
            return Growth(metric, key, current.source, candidate.concept, candidate, comparable,
                          yoy, False)
        return Growth(metric, key, current.source, None, current, None, None, False,
                      ("NO_SAME_SOURCE_COMPARABLE",))

    def latest_key(self, metric: str) -> QuarterKey | None:
        keys = self.effective(metric)
        return max(keys) if keys else None

    def latest_growth(self, metric: str) -> Growth | None:
        key = self.latest_key(metric)
        if key is None:
            return None
        growth = self.growth(metric, key)
        if growth.yoy_pct is None:
            growth = Growth(**{**growth.__dict__, "reasons": growth.reasons + ("LATEST_QUARTER_YOY_UNAVAILABLE",)})
        return growth

    def growth_history(self, metric: str) -> list[Growth]:
        return [
            growth for growth in (self.growth(metric, key) for key in sorted(self.effective(metric)))
            if growth.yoy_pct is not None
        ]

    def source_comparisons(self, metric: str) -> list[SourceComparison]:
        result = []
        for key, values in sorted(self.candidates.get(metric, {}).items()):
            sec = sorted((item for item in values if item.source == "SEC"), key=_priority)
            yahoo = sorted((item for item in values if item.source == "YAHOO"), key=_priority)
            if not sec or not yahoo:
                continue
            first, second = sec[0].value, yahoo[0].value
            scale = max(abs(first), abs(second))
            difference = Decimal(0) if scale == 0 else abs(first - second) / scale * 100
            result.append(SourceComparison(metric, key, sec[0], yahoo[0], difference))
        return result


def build_quarterly_view(
    sec_facts: Iterable[SecFact],
    yahoo_rows: Iterable[Mapping],
    *,
    split_events: Sequence[SplitEvent],
    as_of: datetime,
    window_start: date | None = None,
) -> QuarterlyView:
    """Build the quarterly view of one company from evidence visible at ``as_of``.

    Values cover periods from one year before ``window_start`` (comparables)
    onwards; split events and split-basis checks cover ``window_start``
    onwards. The default window is six years, as in the split capture.
    """

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    as_of_date = as_of.date()
    window_start = window_start or six_years_before(as_of_date)
    facts = [fact for fact in sec_facts if fact.filed_date <= as_of_date]
    value_facts = [fact for fact in facts if fact.period_end >= window_start - timedelta(days=400)]
    events = tuple(event for event in split_events if event.event_date <= as_of_date)
    acc = _Accumulator()
    calendar = build_fiscal_calendar(facts)
    acc.diagnostics.extend(calendar.reasons)
    events, rejected = verify_provider_events(value_facts, events, acc)
    sec_values = _sec_quarter_values(value_facts, calendar, events, as_of_date, acc, window_start)
    derived = _derived_q4_values(value_facts, calendar, sec_values, acc)
    yahoo_values = _yahoo_quarter_values(list(yahoo_rows), facts, events, as_of, acc)

    candidates: dict[str, dict[QuarterKey, list[QuarterValue]]] = defaultdict(lambda: defaultdict(list))
    for item in [*sec_values, *derived, *yahoo_values]:
        candidates[item.metric][item.key].append(item)
    frozen = {
        metric: {key: tuple(sorted(values, key=_priority)) for key, values in by_key.items()}
        for metric, by_key in candidates.items()
    }
    return QuarterlyView(
        as_of=as_of,
        candidates=frozen,
        calendar=calendar,
        diagnostics=tuple(dict.fromkeys(acc.diagnostics)),
        split_status_reasons=tuple(dict.fromkeys(acc.split_reasons)),
        rejected_split_events=rejected,
    )
