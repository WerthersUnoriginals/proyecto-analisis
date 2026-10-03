"""Institutional sponsorship calculations for I (i-institutional-v1, spec 2026-10-03 §6)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Sequence

from database.split_basis import SplitEvent, basis_factor

SPONSORSHIP_VERSION = "i-institutional-v1"
FILING_DEADLINE_DAYS = 45
DEFAULT_QUARTERS = 5
# A holder count that doubles or halves in one quarter is a discontinuity
# (new CUSIP after a reorganization, as XOM in July 2026), not sponsorship.
DISCONTINUITY_MIN_HOLDERS = 20
DISCONTINUITY_MAX_RATIO = Decimal(2)


@dataclass(frozen=True)
class Filing13F:
    accession: str
    filer_cik: str
    filing_date: date
    submission_type: str
    period_of_report: date
    amendment_type: str | None = None


@dataclass(frozen=True)
class Holding13F:
    accession: str
    infotable_sk: str
    cusip: str
    name_of_issuer: str
    title_of_class: str
    value: Decimal
    shares: Decimal
    shares_type: str
    put_call: str | None = None


@dataclass(frozen=True)
class Sponsorship:
    status: str
    latest_quarter: date | None = None
    holders_by_quarter: dict = field(default_factory=dict)
    holders_latest: int | None = None
    holders_change_qoq_pct: Decimal | None = None
    holders_change_yoy_pct: Decimal | None = None
    quarters_increasing: int | None = None
    institutional_shares: Decimal | None = None
    ownership_pct: Decimal | None = None
    diagnostics: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()


def _quarter_end(year: int, quarter: int) -> date:
    month = quarter * 3
    return (date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1))


def complete_quarters(as_of_date: date, count: int) -> list[date]:
    """The latest ``count`` quarter ends whose 13F deadline is on or before ``as_of_date``, latest first."""

    year, quarter = as_of_date.year, (as_of_date.month - 1) // 3 + 1
    result = []
    while len(result) < count:
        end = _quarter_end(year, quarter)
        if end + timedelta(days=FILING_DEADLINE_DAYS) <= as_of_date:
            result.append(end)
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return result


def effective_accessions(filings: Iterable[Filing13F], period: date, as_of_date: date) -> set[str]:
    """Latest original-or-restatement per filer, plus later new-holdings amendments."""

    by_filer: dict[str, list[Filing13F]] = defaultdict(list)
    for item in filings:
        if item.period_of_report == period and item.filing_date <= as_of_date:
            by_filer[item.filer_cik].append(item)
    accessions: set[str] = set()
    for items in by_filer.values():
        bases = [item for item in items if item.submission_type == "13F-HR" or item.amendment_type == "RESTATEMENT"]
        if not bases:
            continue
        base = max(bases, key=lambda item: (item.filing_date, item.accession))
        accessions.add(base.accession)
        accessions.update(
            item.accession for item in items
            if item.amendment_type == "NEW HOLDINGS" and (item.filing_date, item.accession) > (base.filing_date, base.accession)
        )
    return accessions


def _pct_change(current: int, previous: int) -> Decimal | None:
    return None if previous <= 0 else (Decimal(current) / Decimal(previous) - 1) * 100


def compute_sponsorship(
    filings: Sequence[Filing13F],
    holdings: Sequence[Holding13F],
    cusip: str,
    *,
    as_of: datetime,
    shares_outstanding: Decimal | None,
    split_events: Sequence[SplitEvent] = (),
    quarters: int = DEFAULT_QUARTERS,
) -> Sponsorship:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    as_of_date = as_of.date()
    periods = complete_quarters(as_of_date, quarters)
    positions: dict[str, list[Holding13F]] = defaultdict(list)
    for item in holdings:
        if item.cusip == cusip and item.put_call is None and item.shares_type == "SH" and item.shares > 0:
            positions[item.accession].append(item)
    filer_of = {item.accession: item.filer_cik for item in filings}
    observed_periods = {item.period_of_report for item in filings if item.filing_date <= as_of_date}

    holders: dict[date, int] = {}
    shares: dict[date, Decimal] = {}
    for period in periods:
        if period not in observed_periods:
            continue
        accessions = effective_accessions(filings, period, as_of_date)
        held = [item for accession in accessions for item in positions.get(accession, ())]
        holders[period] = len({filer_of[item.accession] for item in held})
        shares[period] = sum((item.shares for item in held), Decimal(0)) * basis_factor(split_events, period, as_of_date)

    diagnostics: list[str] = []
    latest = periods[0] if periods else None
    if latest not in holders:
        return Sponsorship("INSUFFICIENT_HISTORY", latest, holders, diagnostics=("NO_13F_FOR_LATEST_QUARTER",))
    qoq = _pct_change(holders[latest], holders[periods[1]]) if len(periods) > 1 and periods[1] in holders else None
    yoy = _pct_change(holders[latest], holders[periods[4]]) if len(periods) > 4 and periods[4] in holders else None
    increasing = 0
    for current, previous in zip(periods, periods[1:]):
        if current in holders and previous in holders and holders[current] > holders[previous]:
            increasing += 1
        else:
            break
    ownership = None
    if shares_outstanding is not None and shares_outstanding > 0:
        ownership = shares[latest] / shares_outstanding * 100
        if ownership > 100:
            diagnostics.append("OWNERSHIP_ABOVE_100_PCT")
    review: list[str] = []
    for current, previous in zip(periods, periods[1:]):
        if current not in holders or previous not in holders:
            continue
        low, high = sorted((holders[current], holders[previous]))
        if high >= DISCONTINUITY_MIN_HOLDERS and (low == 0 or Decimal(high) / low >= DISCONTINUITY_MAX_RATIO):
            review.append("HOLDERS_DISCONTINUITY")
            diagnostics.append(f"HOLDERS_DISCONTINUITY:{current.isoformat()}")
    complete = all(period in holders for period in periods)
    if not complete:
        diagnostics.append("MISSING_13F_QUARTERS")
    return Sponsorship(
        status="OK" if complete else "INSUFFICIENT_HISTORY",
        latest_quarter=latest,
        holders_by_quarter=holders,
        holders_latest=holders[latest],
        holders_change_qoq_pct=qoq,
        holders_change_yoy_pct=yoy,
        quarters_increasing=increasing,
        institutional_shares=shares[latest],
        ownership_pct=ownership,
        diagnostics=tuple(diagnostics),
        review_reasons=tuple(dict.fromkeys(review)),
    )
