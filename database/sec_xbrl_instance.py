"""Read catalog facts from a filing's own XBRL instance (sec-xbrl-instance-v1).

SEC's Company Facts API sometimes lags weeks behind EDGAR: the 10-Q and its
XBRL are public, but the API does not include them yet (PLD and NEE Q2 2026).
This module reads the extracted instance EDGAR publishes with each filing and
applies the Company Facts conventions: non-dimensional contexts of the
registrant's own CIK only (co-registrant and segment figures carry
dimensions), catalog tags only, values as published.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable

from database.sec_facts import TAG_TO_METRIC, TAXONOMY, SecFact

INSTANCE_ORIGIN = "sec.xbrl_instance"
XBRLI = "{http://www.xbrl.org/2003/instance}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
US_GAAP_PREFIX = "http://fasb.org/us-gaap/"
DEI_PREFIX = "http://xbrl.sec.gov/dei/"
_LINKBASE_SUFFIXES = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_ref.xml")


def choose_instance_file(names: Iterable[str]) -> str | None:
    """Pick the instance document from a filing directory listing."""

    names = list(names)
    extracted = [name for name in names if name.endswith("_htm.xml")]
    if len(extracted) == 1:
        return extracted[0]
    candidates = [
        name for name in names
        if name.endswith(".xml") and not name.endswith(_LINKBASE_SUFFIXES)
        and name != "FilingSummary.xml" and not name.endswith("_htm.xml")
    ]
    return candidates[0] if len(candidates) == 1 else None


def _split(tag: str) -> tuple[str, str]:
    namespace, _, local = tag[1:].partition("}")
    return namespace, local


def _measure(element) -> str:
    text = (element.text or "").strip()
    local = text.split(":", 1)[-1]
    return local if local in {"USD", "shares", "pure"} else text


def _units(root) -> dict[str, str]:
    units = {}
    for unit in root.iter(f"{XBRLI}unit"):
        divide = unit.find(f"{XBRLI}divide")
        if divide is not None:
            numerator = divide.find(f"{XBRLI}unitNumerator/{XBRLI}measure")
            denominator = divide.find(f"{XBRLI}unitDenominator/{XBRLI}measure")
            units[unit.get("id")] = f"{_measure(numerator)}/{_measure(denominator)}"
        else:
            units[unit.get("id")] = _measure(unit.find(f"{XBRLI}measure"))
    return units


def _contexts(root, cik: str) -> dict[str, tuple[date | None, date]]:
    """Non-dimensional contexts of ``cik`` only: context id -> (start, end)."""

    contexts = {}
    for context in root.iter(f"{XBRLI}context"):
        entity = context.find(f"{XBRLI}entity")
        identifier = entity.find(f"{XBRLI}identifier") if entity is not None else None
        if identifier is None or (identifier.text or "").strip().zfill(10) != cik:
            continue
        if entity.find(f"{XBRLI}segment") is not None or context.find(f"{XBRLI}scenario") is not None:
            continue
        period = context.find(f"{XBRLI}period")
        instant = period.find(f"{XBRLI}instant")
        if instant is not None:
            contexts[context.get("id")] = (None, date.fromisoformat(instant.text.strip()[:10]))
        else:
            start = period.find(f"{XBRLI}startDate").text.strip()[:10]
            end = period.find(f"{XBRLI}endDate").text.strip()[:10]
            contexts[context.get("id")] = (date.fromisoformat(start), date.fromisoformat(end))
    return contexts


def parse_xbrl_instance(
    content: bytes, *, cik: str, accession: str, form: str, filed_date: date,
) -> list[SecFact]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise ValueError("invalid XBRL instance document") from None
    contexts = _contexts(root, cik)
    units = _units(root)
    fiscal_year = fiscal_period = None
    for element in root:
        namespace, local = _split(element.tag) if element.tag.startswith("{") else ("", element.tag)
        if namespace.startswith(DEI_PREFIX) and element.get("contextRef") in contexts:
            if local == "DocumentFiscalYearFocus":
                fiscal_year = int(element.text.strip())
            elif local == "DocumentFiscalPeriodFocus":
                fiscal_period = element.text.strip()

    facts: dict[tuple, SecFact] = {}
    for element in root:
        if not element.tag.startswith("{"):
            continue
        namespace, local = _split(element.tag)
        if not namespace.startswith(US_GAAP_PREFIX) or local not in TAG_TO_METRIC:
            continue
        if element.get(XSI_NIL) == "true" or element.get("contextRef") not in contexts:
            continue
        try:
            value = Decimal((element.text or "").strip())
        except InvalidOperation:
            continue
        start, end = contexts[element.get("contextRef")]
        fact = SecFact(
            taxonomy=TAXONOMY, tag=local, unit=units.get(element.get("unitRef"), ""),
            period_start=start, period_end=end, value=value, accession=accession,
            fiscal_year=fiscal_year, fiscal_period=fiscal_period, form=form,
            filed_date=filed_date, frame=None, origin=INSTANCE_ORIGIN,
        )
        existing = facts.get(fact.identity)
        if existing is not None and existing.value != fact.value:
            raise ValueError(f"inconsistent duplicate facts in instance: {fact.identity}")
        facts[fact.identity] = fact
    return sorted(facts.values(), key=lambda fact: (fact.tag, fact.period_end, fact.period_start or date.min))
