"""Informative, unscored SEC catalysts for N (sec-catalysts-v1, spec 2026-10-03 §7).

Official 8-K items only, reported literally: a 5.02 can be a departure or an
appointment and is never interpreted. Catalysts never change a score, a
status, or usability.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

CATALYSTS_VERSION = "sec-catalysts-v1"
CATALYST_ITEMS = {"5.02": "MANAGEMENT_CHANGE", "2.01": "ACQUISITION_OR_DISPOSITION"}
CATALYST_FORMS = frozenset({"8-K", "8-K/A"})
WINDOW_DAYS = 365
EDGAR_TIMEZONE = ZoneInfo("America/New_York")
EDGAR_FOLDER_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/"


@dataclass(frozen=True)
class CatalystFiling:
    cik: str
    accession: str
    form: str
    filing_date: date
    acceptance_at: datetime | None
    items: tuple[str, ...]


def available_at(filing: CatalystFiling) -> datetime:
    """Acceptance time, or the end of the EDGAR filing day when it is missing.

    The fallback is never earlier than the real acceptance, so it cannot leak
    a filing into an earlier ``as_of``.
    """

    if filing.acceptance_at is not None:
        return filing.acceptance_at
    return datetime.combine(filing.filing_date + timedelta(days=1), time(), EDGAR_TIMEZONE)


def select_catalysts(filings: Iterable[CatalystFiling], as_of: datetime) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    window_start = as_of.date() - timedelta(days=WINDOW_DAYS)
    events, diagnostics = [], []
    for filing in sorted(filings, key=lambda item: (item.filing_date, item.accession), reverse=True):
        if filing.form not in CATALYST_FORMS or filing.filing_date < window_start:
            continue
        if available_at(filing) > as_of:
            continue
        selected = [item for item in filing.items if item in CATALYST_ITEMS]
        if selected and filing.acceptance_at is None:
            diagnostics.append(f"ACCEPTANCE_MISSING:{filing.accession}")
        for item in selected:
            events.append({
                "item": item,
                "category": CATALYST_ITEMS[item],
                "form": filing.form,
                "filing_date": filing.filing_date.isoformat(),
                "accession": filing.accession,
                "url": EDGAR_FOLDER_URL.format(cik=int(filing.cik), folder=filing.accession.replace("-", "")),
            })
    return {
        "version": CATALYSTS_VERSION,
        "window_days": WINDOW_DAYS,
        "events": events,
        "counts": dict(Counter(event["item"] for event in events)),
        "diagnostics": diagnostics,
    }
