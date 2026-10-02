"""Split-event reconciliation and per-share basis conversion (split-basis-v1).

SEC restates comparatives after a split, but never re-reports older periods,
so values selected by "latest filing" mix bases. This module reconciles split
events from the market-data provider with SEC's own split-ratio facts and
converts any value to the share basis in force at ``as_of``.

A value's basis is the basis at the moment it was published: the filing date
for SEC (statements are restated when a split precedes their issuance) and the
observation date for Yahoo (which restates history retroactively).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Sequence

from database.sec_facts import SecFact

SPLIT_BASIS_VERSION = "split-basis-v1"

SEC_MATCH_DAYS = 45
# One split is often tagged at several dates (approval, record, effective).
SAME_EVENT_DAYS = 120
UNCERTAIN_BASIS_DAYS = 7
SUSPECTED_RATIO_TOLERANCE = Decimal("0.02")
COMMON_SPLIT_RATIOS = tuple(
    Decimal(value) for value in ("1.5", "2", "3", "4", "5", "8", "10", "15", "20")
)


@dataclass(frozen=True)
class SplitEvent:
    event_date: date
    ratio: Decimal
    sources: tuple[str, ...]


@dataclass(frozen=True)
class SplitReconciliation:
    status: str
    events: tuple[SplitEvent, ...]
    reasons: tuple[str, ...] = ()
    version: str = SPLIT_BASIS_VERSION


def _sec_interval(fact: SecFact) -> tuple[date, date]:
    start = fact.period_start or fact.period_end
    return start - timedelta(days=SEC_MATCH_DAYS), fact.period_end + timedelta(days=SEC_MATCH_DAYS)


def _sec_split_facts(facts: Iterable[SecFact], window_start: date, as_of: date) -> list[SecFact]:
    return [
        fact for fact in facts
        if fact.tag == "StockholdersEquityNoteStockSplitConversionRatio1"
        and fact.value > 0
        and fact.value != 1
        and window_start <= fact.period_end <= as_of
    ]


def reconcile_split_events(
    provider_events: Sequence[tuple[date, Decimal]] | None,
    sec_facts: Iterable[SecFact],
    *,
    window_start: date,
    as_of: date,
) -> SplitReconciliation:
    """Reconcile provider split events with SEC split-ratio facts.

    ``provider_events=None`` means no successful provider capture is visible.
    Every event is returned for adjustment; any disagreement between sources
    makes the status ``REVIEW_REQUIRED``.
    """

    sec_ratio_facts = _sec_split_facts(sec_facts, window_start, as_of)
    reasons: list[str] = []
    events: list[SplitEvent] = []
    matched_sec: set[int] = set()

    for event_date, ratio in sorted(provider_events or ()):
        if not window_start <= event_date <= as_of:
            continue
        ratio = Decimal(ratio)
        nearby = [
            index for index, fact in enumerate(sec_ratio_facts)
            if _sec_interval(fact)[0] <= event_date <= _sec_interval(fact)[1]
        ]
        agreeing = [index for index in nearby if sec_ratio_facts[index].value == ratio]
        matched_sec.update(nearby)
        if agreeing:
            events.append(SplitEvent(event_date, ratio, ("SEC", "YAHOO")))
        else:
            if nearby:
                reasons.append("SPLIT_RATIO_DISAGREEMENT")
            else:
                reasons.append("PROVIDER_SPLIT_NOT_IN_SEC")
            events.append(SplitEvent(event_date, ratio, ("YAHOO",)))

    unmatched = [
        fact for index, fact in enumerate(sec_ratio_facts) if index not in matched_sec
    ]
    for fact in sorted(unmatched, key=lambda item: (item.period_end, item.filed_date)):
        start = (fact.period_start or fact.period_end) - timedelta(days=SAME_EVENT_DAYS)
        end = fact.period_end + timedelta(days=SAME_EVENT_DAYS)
        if any(event.ratio == fact.value and start <= event.event_date <= end for event in events):
            continue
        reasons.append("SEC_SPLIT_NOT_IN_PROVIDER")
        events.append(SplitEvent(fact.period_end, fact.value, ("SEC",)))

    events.sort(key=lambda item: item.event_date)
    hard_reasons = {"SPLIT_RATIO_DISAGREEMENT", "SEC_SPLIT_NOT_IN_PROVIDER"}
    if any(reason in hard_reasons for reason in reasons):
        status = "REVIEW_REQUIRED"
    elif provider_events is None:
        status = "UNKNOWN"
    elif not events:
        status = "NO_RECENT_SPLITS"
    else:
        # Provider-only events are confirmed later by restated SEC pairs; the
        # caller downgrades the status if those pairs contradict the event.
        status = "VERIFIED_ALREADY_ADJUSTED"
    return SplitReconciliation(status, tuple(events), tuple(dict.fromkeys(reasons)))


def basis_factor(events: Iterable[SplitEvent], basis_date: date, as_of: date) -> Decimal:
    """Number of as-of shares per share at ``basis_date``."""

    factor = Decimal(1)
    for event in events:
        if basis_date < event.event_date <= as_of:
            factor *= event.ratio
    return factor


def basis_uncertain(events: Iterable[SplitEvent], basis_date: date) -> bool:
    """True when publication is too close to an event to know its basis."""

    return any(
        abs((basis_date - event.event_date).days) <= UNCERTAIN_BASIS_DAYS
        for event in events
    )


def suspected_split_ratio(earlier: Decimal, later: Decimal) -> Decimal | None:
    """Return ``earlier/later`` when it matches a common split ratio or inverse."""

    if earlier == 0 or later == 0 or (earlier < 0) != (later < 0):
        return None
    observed = earlier / later
    for ratio in COMMON_SPLIT_RATIOS:
        for candidate in (ratio, Decimal(1) / ratio):
            if abs(observed / candidate - 1) <= SUSPECTED_RATIO_TOLERANCE:
                return candidate.normalize() if candidate >= 1 else Decimal(1) / ratio
    return None
