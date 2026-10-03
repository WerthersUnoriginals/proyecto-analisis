"""Registrant profile from official EDGAR filing metadata (registrant-v1).

The C and A contracts consume SEC 10-K/10-Q evidence only. A foreign private
issuer files annual reports on Form 20-F or 40-F and interim reports on 6-K
(usually without standard XBRL), so it is reported explicitly as unsupported
instead of looking like missing data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

REGISTRANT_VERSION = "registrant-v1"
LOOKBACK_DAYS = 1096
DOMESTIC_PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-QT"})
FOREIGN_ANNUAL_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})


def classify_filer(filings: Iterable[tuple[str, date]], as_of: datetime) -> str:
    """Return DOMESTIC, FOREIGN_FILER_NOT_SUPPORTED, or NO_PERIODIC_FILINGS."""

    as_of_date = as_of.date()
    start = as_of_date - timedelta(days=LOOKBACK_DAYS)
    recent = [form for form, filed in filings if start <= filed <= as_of_date]
    if any(form in DOMESTIC_PERIODIC_FORMS for form in recent):
        return "DOMESTIC"
    if any(form in FOREIGN_ANNUAL_FORMS for form in recent):
        return "FOREIGN_FILER_NOT_SUPPORTED"
    return "NO_PERIODIC_FILINGS"


# --------------------------------------------------------------------------
# Successor registrants (holding-company reorganizations)
# --------------------------------------------------------------------------
#
# A reorganization can move a company to a new SEC registrant (new CIK) while
# its history stays under the old one (ExxonMobil Holdings Corp, 2026-07-01).
# The official evidence is the successor's succession filing (Rule 12g-3:
# Form 8-K12B or 8-K12G3) and the periodic reports filed jointly by both
# registrants. The predecessor is linked only when both are present and it has
# periodic history of its own; otherwise the succession is reported as
# unresolved and the analysis goes to review.

SUCCESSION_FORMS = frozenset({"8-K12B", "8-K12B/A", "8-K12G3", "8-K12G3/A"})
# Analyses use at most seven years of evidence; an older succession cannot
# affect any calculation.
RELEVANT_SUCCESSION_DAYS = round(365.25 * 7)
PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
_FILER_PATTERN = re.compile(
    r"COMPANY CONFORMED NAME:\s*(?P<name>[^\n]+?)\s*\n.*?CENTRAL INDEX KEY:\s*(?P<cik>\d+)", re.S,
)


@dataclass(frozen=True)
class RegistrantLink:
    successor_cik: str
    predecessor_cik: str | None
    succession_form: str
    succession_accession: str
    succession_date: date
    status: str  # LINKED or PREDECESSOR_NOT_FOUND


def parse_filers_header(text: str) -> list[tuple[str, str]]:
    """(cik, name) of every filer listed in an EDGAR filing header."""

    filers = []
    for block in text.split("FILER:")[1:]:
        match = _FILER_PATTERN.search(block)
        if match:
            filers.append((match.group("cik").zfill(10), match.group("name").strip()))
    return filers


def succession_filings(filings: Iterable[tuple[str, date, str]]) -> list[tuple[str, date, str]]:
    return sorted((item for item in filings if item[0] in SUCCESSION_FORMS), key=lambda item: item[1])


def choose_predecessors(
    successor_cik: str,
    succession_date: date,
    joint_filers: dict[str, list[str]],
    histories: dict[str, list[tuple[str, date]]],
) -> list[str]:
    """Co-filers of the successor's periodic reports with 10-K history before succession."""

    candidates = sorted({cik for filers in joint_filers.values() for cik in filers if cik != successor_cik})
    return [
        cik for cik in candidates
        if any(form in ("10-K", "10-K/A") and filed < succession_date for form, filed in histories.get(cik, ()))
    ]


def registrant_history(
    links: Iterable[RegistrantLink], as_of: datetime | None = None,
) -> tuple[list[dict], list[str], bool]:
    """Entries for the analysis, diagnostics, and whether review is required."""

    entries, diagnostics, review = [], [], False
    for link in sorted(links, key=lambda item: item.succession_date):
        relevant = as_of is None or (as_of.date() - link.succession_date).days <= RELEVANT_SUCCESSION_DAYS
        entries.append({
            "successor_cik": link.successor_cik,
            "predecessor_cik": link.predecessor_cik,
            "succession_date": link.succession_date.isoformat(),
            "succession_form": link.succession_form,
            "succession_accession": link.succession_accession,
            "status": link.status,
            "affects_analysis_window": relevant,
        })
        if link.status == "LINKED":
            diagnostics.append(
                f"REGISTRANT_SUCCESSION:{link.succession_date.isoformat()}:"
                f"{link.predecessor_cik}->{link.successor_cik}"
            )
        elif relevant:
            diagnostics.append("REGISTRANT_SUCCESSION_UNRESOLVED")
            review = True
        else:
            diagnostics.append("REGISTRANT_SUCCESSION_OUTSIDE_WINDOW")
    return entries, diagnostics, review
