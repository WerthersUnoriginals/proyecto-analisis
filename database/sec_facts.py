"""Verbatim SEC Company Facts evidence for the v3 fundamentals pipeline.

Raw facts are stored exactly as SEC publishes them for every tag in a
versioned catalog, across all forms and durations. Mapping a tag to a metric,
classifying periods, and choosing values are normalization concerns handled
downstream on read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

CATALOG_VERSION = "sec-tag-catalog-v7"

# Ordered by preference within each metric.
SEC_TAG_CATALOG: Mapping[str, tuple[str, ...]] = {
    "EPS_DILUTED": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
    "EPS_BASIC": ("EarningsPerShareBasic",),
    # Totals first. Contract revenue (ASC 606) is a subset when a company also
    # reports a total (BRK: Revenues 101.8B vs contract revenue 70.1B); banks
    # report net revenue and utilities operating revenue under their own tags.
    "REVENUE": (
        "Revenues",
        "RevenuesNetOfInterestExpense",
        "RegulatedAndUnregulatedOperatingRevenue",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "NET_INCOME": ("NetIncomeLoss", "ProfitLoss"),
    "DILUTED_SHARES": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
    ),
    # Basic weighted shares: S falls back to them when a company stopped
    # tagging diluted shares (XOM since 2013, where both are equal).
    "BASIC_SHARES": ("WeightedAverageNumberOfSharesOutstandingBasic",),
    "SPLIT_RATIO": ("StockholdersEquityNoteStockSplitConversionRatio1",),
    "STOCKHOLDERS_EQUITY": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    # Debt components for S (spec 2026-10-03-supply-demand-s-v1 §4). Each tag
    # is a different concept; S combines them only through fixed definitions.
    "DEBT": (
        "LongTermDebt",
        "LongTermDebtNoncurrent",
        "LongTermDebtCurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
        "DebtLongtermAndShorttermCombinedAmount",
    ),
}

METRIC_UNITS: Mapping[str, str] = {
    "EPS_DILUTED": "USD/shares",
    "EPS_BASIC": "USD/shares",
    "REVENUE": "USD",
    "NET_INCOME": "USD",
    "DILUTED_SHARES": "shares",
    "BASIC_SHARES": "shares",
    "SPLIT_RATIO": "pure",
    "STOCKHOLDERS_EQUITY": "USD",
    "DEBT": "USD",
}

TAG_TO_METRIC: Mapping[str, str] = {
    tag: metric for metric, tags in SEC_TAG_CATALOG.items() for tag in tags
}

TAXONOMY = "us-gaap"
PERIODIC_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A"})

_DURATION_CLASSES = (
    ("QUARTER", 70, 110),
    ("YTD_6M", 160, 200),
    ("YTD_9M", 250, 290),
    ("ANNUAL", 330, 400),
)


@dataclass(frozen=True)
class SecFact:
    taxonomy: str
    tag: str
    unit: str
    period_start: date | None
    period_end: date
    value: Decimal
    accession: str
    fiscal_year: int | None
    fiscal_period: str | None
    form: str
    filed_date: date
    frame: str | None
    observed_at: datetime | None = None
    id: int | None = None
    origin: str = "sec.company_facts"

    @property
    def identity(self) -> tuple:
        return (
            self.taxonomy, self.tag, self.unit,
            self.period_start, self.period_end, self.accession,
        )

    @property
    def metric(self) -> str | None:
        return TAG_TO_METRIC.get(self.tag)


def duration_class(fact: SecFact) -> str:
    """Classify a fact by its reported interval; never by form or fiscal period."""

    if fact.period_start is None:
        return "INSTANT"
    days = (fact.period_end - fact.period_start).days
    for name, low, high in _DURATION_CLASSES:
        if low <= days <= high:
            return name
    return "OTHER"


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("SEC fact value must be numeric")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("SEC fact value must be finite")
        return Decimal(repr(value))
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("SEC fact value must be finite")
    return result


def _date(value) -> date | None:
    return None if value in (None, "") else date.fromisoformat(str(value)[:10])


def _fiscal_year(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def parse_companyfacts(payload: Mapping) -> list[SecFact]:
    """Return catalog facts verbatim; identical duplicates collapse, conflicts raise."""

    concepts = payload.get("facts", {}).get(TAXONOMY, {})
    by_identity: dict[tuple, SecFact] = {}
    for tag in TAG_TO_METRIC:
        concept = concepts.get(tag)
        if not concept:
            continue
        for unit, items in concept.get("units", {}).items():
            for raw in items:
                fact = SecFact(
                    taxonomy=TAXONOMY,
                    tag=tag,
                    unit=unit,
                    period_start=_date(raw.get("start")),
                    period_end=_date(raw["end"]),
                    value=_decimal(raw.get("val")),
                    accession=str(raw["accn"]),
                    fiscal_year=_fiscal_year(raw.get("fy")),
                    fiscal_period=raw.get("fp"),
                    form=str(raw.get("form") or ""),
                    filed_date=_date(raw["filed"]),
                    frame=raw.get("frame"),
                )
                existing = by_identity.get(fact.identity)
                if existing is None:
                    by_identity[fact.identity] = fact
                elif existing.value != fact.value:
                    raise ValueError(f"conflicting SEC fact values for {fact.identity}")
    return sorted(
        by_identity.values(),
        key=lambda fact: (fact.tag, fact.unit, fact.period_end, fact.period_start or date.min, fact.accession),
    )
