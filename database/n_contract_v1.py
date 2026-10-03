"""N input contract v1 built from the price series and highs (spec 2026-10-03-new-highs-n-v1 §8)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Sequence

from database.catalysts_v3 import CATALYSTS_VERSION
from database.new_highs_v1 import NEW_HIGHS_VERSION, HighsResult
from database.prices_v3 import PRICE_SERIES_VERSION, PriceSeries
from database.split_basis import SPLIT_BASIS_VERSION, SplitEvent, SplitReconciliation, split_events_provenance

CONTRACT_VERSION = "n-input-contract-v1"
REVIEW_SPLIT_STATUSES = frozenset({"REVIEW_REQUIRED", "UNKNOWN", "UNADJUSTED_DETECTED"})

N_INPUT_KEYS = (
    "last_bar_date",
    "close_last",
    "high_52w",
    "pct_below_high_52w",
    "new_high_recent",
    "sessions_since_high_52w",
    "high_5y",
    "pct_below_high_5y",
    "sessions_available",
    "n_status",
    "price_data_integrity",
    "split_integrity_status",
)
FIFTY_TWO_WEEK_KEYS = ("high_52w", "pct_below_high_52w", "new_high_recent", "sessions_since_high_52w")
FIVE_YEAR_KEYS = ("high_5y", "pct_below_high_5y")


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def build_n_contract_v1(
    series: PriceSeries,
    highs: HighsResult,
    splits: SplitReconciliation,
    *,
    split_status: str,
    split_reasons: Sequence[str],
    rejected_splits: Sequence[SplitEvent],
    company_id: int,
    as_of: datetime,
    filer_status: str = "DOMESTIC",
    catalysts: dict,
) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    diagnostics = list(series.diagnostics) + list(series.review_reasons) + list(highs.diagnostics)
    diagnostics += list(highs.review_reasons)

    if filer_status != "DOMESTIC":
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(filer_status)
    elif split_status in REVIEW_SPLIT_STATUSES:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(f"SPLIT_STATUS:{split_status}")
    elif series.review_reasons or highs.review_reasons:
        integrity = "REVIEW_REQUIRED"
    elif highs.status in {"NO_PRICE_EVIDENCE", "STALE_PRICES"}:
        integrity = "REVIEW_REQUIRED"
    elif highs.status == "INSUFFICIENT_HISTORY" or highs.high_5y is None:
        integrity = "VERIFIED_WITH_PARTIAL_CORE_DATA"
    else:
        integrity = "VERIFIED"

    values = {
        "last_bar_date": highs.last_bar_date.isoformat() if highs.last_bar_date else None,
        "close_last": _number(highs.close_last),
        "high_52w": _number(highs.high_52w),
        "pct_below_high_52w": _number(highs.pct_below_high_52w),
        "new_high_recent": highs.new_high_recent,
        "sessions_since_high_52w": highs.sessions_since_high_52w,
        "high_5y": _number(highs.high_5y),
        "pct_below_high_5y": _number(highs.pct_below_high_5y),
        "sessions_available": highs.sessions_available,
        "n_status": highs.status,
        "price_data_integrity": integrity,
        "split_integrity_status": split_status,
    }
    last = series.bars[-1] if series.bars and highs.last_bar_date else None
    reasons_52w = [] if highs.high_52w is not None else [
        item for item in highs.diagnostics if item in {"NO_PRICE_EVIDENCE", "INSUFFICIENT_PRICE_HISTORY"}
    ]
    reasons_5y = [] if highs.high_5y is not None else (
        reasons_52w or [item for item in highs.diagnostics if item == "HIGH_5Y_INSUFFICIENT_HISTORY"]
    )
    provenance = {key: {} for key in N_INPUT_KEYS}
    provenance.update({key: {"reasons": reasons_52w} for key in FIFTY_TWO_WEEK_KEYS})
    provenance.update({key: {"reasons": reasons_5y} for key in FIVE_YEAR_KEYS})
    provenance["close_last"] = {} if last is None else {
        "observed_at": last.observed_at.isoformat(), "basis_factor": str(last.basis_factor), "raw_id": last.raw_id,
    }
    provenance["price_data_integrity"] = {"reasons": list(dict.fromkeys(diagnostics))}
    provenance["split_integrity_status"] = {
        "reasons": list(dict.fromkeys(split_reasons)),
        "events": split_events_provenance(splits, rejected_splits),
    }
    as_of_text = as_of.isoformat()
    return {
        **values,
        "company_id": company_id,
        "as_of": as_of_text,
        "catalysts": catalysts,
        "n_input_contract": {
            "version": CONTRACT_VERSION,
            "price_series_version": PRICE_SERIES_VERSION,
            "highs_version": NEW_HIGHS_VERSION,
            "split_basis_version": SPLIT_BASIS_VERSION,
            "catalysts_version": CATALYSTS_VERSION,
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": {key: {"value": values[key], **provenance[key]} for key in N_INPUT_KEYS},
        },
        "integrity": {
            "price_data_integrity": integrity,
            "split_integrity_status": split_status,
            "filer_status": filer_status,
            "diagnostics": list(dict.fromkeys(diagnostics)),
        },
    }
