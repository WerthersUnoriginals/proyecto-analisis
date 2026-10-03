"""I input contract v1 (spec 2026-10-03-institutional-i-v1 §7)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Sequence

from database.institutional_v1 import CUSIP_RESOLUTION_VERSION
from database.split_basis import SPLIT_BASIS_VERSION, SplitEvent, SplitReconciliation, split_events_provenance
from database.sponsorship_v1 import SPONSORSHIP_VERSION, Sponsorship

CONTRACT_VERSION = "i-input-contract-v1"
REVIEW_SPLIT_STATUSES = frozenset({"REVIEW_REQUIRED", "UNKNOWN", "UNADJUSTED_DETECTED"})
REVIEW_CUSIP_SOURCES = frozenset({"CUSIP_SOURCES_DISAGREE", "CUSIP_AMBIGUOUS", "CUSIP_NOT_FOUND"})

I_INPUT_KEYS = (
    "cusip",
    "cusip_source",
    "latest_quarter",
    "holders_latest",
    "holders_change_qoq_pct",
    "holders_change_yoy_pct",
    "quarters_increasing",
    "holders_by_quarter",
    "institutional_shares",
    "institutional_ownership_pct",
    "i_data_integrity",
    "split_integrity_status",
)


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def build_i_contract_v1(
    sponsorship: Sponsorship,
    splits: SplitReconciliation,
    *,
    cusip: str | None,
    cusip_source: str | None,
    split_status: str,
    split_reasons: Sequence[str],
    rejected_splits: Sequence[SplitEvent],
    company_id: int,
    as_of: datetime,
    filer_status: str = "DOMESTIC",
) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    diagnostics = list(sponsorship.diagnostics)
    if cusip_source:
        diagnostics.append(cusip_source)
    else:
        diagnostics.append("CUSIP_NOT_RESOLVED")

    if filer_status != "DOMESTIC":
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(filer_status)
    elif cusip is None or cusip_source in REVIEW_CUSIP_SOURCES or cusip_source is None:
        integrity = "REVIEW_REQUIRED"
    elif split_status in REVIEW_SPLIT_STATUSES:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(f"SPLIT_STATUS:{split_status}")
    elif sponsorship.status != "OK" or sponsorship.review_reasons:
        integrity = "REVIEW_REQUIRED"
    elif sponsorship.ownership_pct is None:
        integrity = "VERIFIED_WITH_PARTIAL_CORE_DATA"
        diagnostics.append("NO_SHARE_COUNT_FOR_OWNERSHIP")
    else:
        integrity = "VERIFIED"

    values = {
        "cusip": cusip,
        "cusip_source": cusip_source,
        "latest_quarter": sponsorship.latest_quarter.isoformat() if sponsorship.latest_quarter else None,
        "holders_latest": sponsorship.holders_latest,
        "holders_change_qoq_pct": _number(sponsorship.holders_change_qoq_pct),
        "holders_change_yoy_pct": _number(sponsorship.holders_change_yoy_pct),
        "quarters_increasing": sponsorship.quarters_increasing,
        "holders_by_quarter": {quarter.isoformat(): count for quarter, count in sponsorship.holders_by_quarter.items()},
        "institutional_shares": _number(sponsorship.institutional_shares),
        "institutional_ownership_pct": _number(sponsorship.ownership_pct),
        "i_data_integrity": integrity,
        "split_integrity_status": split_status,
    }
    provenance = {key: {} for key in I_INPUT_KEYS}
    provenance.update({
        "holders_latest": {"status": sponsorship.status},
        "institutional_ownership_pct": {
            "basis": "13F shares on the as-of split basis / latest weighted shares (approximate)",
        },
        "i_data_integrity": {"reasons": list(dict.fromkeys(diagnostics))},
        "split_integrity_status": {
            "reasons": list(dict.fromkeys(split_reasons)),
            "events": split_events_provenance(splits, rejected_splits),
        },
    })
    as_of_text = as_of.isoformat()
    return {
        **values,
        "company_id": company_id,
        "as_of": as_of_text,
        "i_input_contract": {
            "version": CONTRACT_VERSION,
            "calculation_version": SPONSORSHIP_VERSION,
            "cusip_resolution_version": CUSIP_RESOLUTION_VERSION,
            "split_basis_version": SPLIT_BASIS_VERSION,
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": {key: {"value": values[key], **provenance[key]} for key in I_INPUT_KEYS},
        },
        "integrity": {
            "i_data_integrity": integrity,
            "split_integrity_status": split_status,
            "filer_status": filer_status,
            "diagnostics": list(dict.fromkeys(diagnostics)),
        },
    }
