"""L input contract v1 (spec 2026-10-03-leader-laggard-l-v1 §6–§7)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from database.prices_v3 import PRICE_SERIES_VERSION, PriceSeries
from database.relative_strength_v1 import (
    MIN_SCORED_UNIVERSE,
    RELATIVE_STRENGTH_VERSION,
    GroupStrength,
    RsLine,
    WeightedScore,
)
from database.split_basis import SPLIT_BASIS_VERSION, SplitEvent, SplitReconciliation, split_events_provenance
from database.universe_v1 import UNIVERSE_NAME

CONTRACT_VERSION = "l-input-contract-v1"
REVIEW_SPLIT_STATUSES = frozenset({"REVIEW_REQUIRED", "UNKNOWN", "UNADJUSTED_DETECTED"})
RS_LINE_LEAD_MIN_PRICE_GAP_PCT = Decimal(5)

L_INPUT_KEYS = (
    "rs_rating",
    "weighted_return_pct",
    "universe_as_of",
    "universe_scored",
    "sic",
    "sic_description",
    "group_key",
    "group_size",
    "group_rank_pct",
    "rank_in_group_pct",
    "rs_line_pct_below_high_52w",
    "rs_line_new_high_recent",
    "l_data_integrity",
    "split_integrity_status",
)


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def build_l_contract_v1(
    score: WeightedScore,
    rating: int | None,
    group: GroupStrength,
    line: RsLine,
    series: PriceSeries,
    splits: SplitReconciliation,
    *,
    split_status: str,
    split_reasons: Sequence[str],
    rejected_splits: Sequence[SplitEvent],
    universe_as_of: date | None,
    universe_scored: int,
    universe_excluded: int,
    sic: str | None,
    sic_description: str | None,
    company_id: int,
    as_of: datetime,
    filer_status: str = "DOMESTIC",
    price_pct_below_high_52w: Decimal | None = None,
) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    diagnostics: list[str] = list(series.diagnostics) + list(series.review_reasons)
    universe_complete = universe_scored >= MIN_SCORED_UNIVERSE
    if not universe_complete:
        diagnostics.append("UNIVERSE_INCOMPLETE")
    if score.status != "OK":
        diagnostics.append(score.status)
    if group.status != "OK":
        diagnostics.append(group.status)
    if line.status != "OK":
        diagnostics.append(f"RS_LINE_{line.status}")
    if (line.at_high and price_pct_below_high_52w is not None
            and price_pct_below_high_52w > RS_LINE_LEAD_MIN_PRICE_GAP_PCT):
        diagnostics.append("RS_LINE_HIGH_BEFORE_PRICE")

    if filer_status != "DOMESTIC":
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(filer_status)
    elif split_status in REVIEW_SPLIT_STATUSES:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(f"SPLIT_STATUS:{split_status}")
    elif series.review_reasons or not universe_complete or score.status == "STALE_PRICES":
        integrity = "REVIEW_REQUIRED"
    elif rating is None or group.status != "OK" or line.status != "OK":
        integrity = "VERIFIED_WITH_PARTIAL_CORE_DATA"
    else:
        integrity = "VERIFIED"

    values = {
        "rs_rating": rating,
        "weighted_return_pct": _number((score.value - 1) * 100) if score.value is not None else None,
        "universe_as_of": universe_as_of.isoformat() if universe_as_of else None,
        "universe_scored": universe_scored,
        "sic": sic,
        "sic_description": sic_description,
        "group_key": group.group_key,
        "group_size": group.group_size,
        "group_rank_pct": _number(group.group_rank_pct),
        "rank_in_group_pct": _number(group.rank_in_group_pct),
        "rs_line_pct_below_high_52w": _number(line.pct_below_high_52w),
        "rs_line_new_high_recent": line.new_high_recent,
        "l_data_integrity": integrity,
        "split_integrity_status": split_status,
    }
    provenance = {key: {} for key in L_INPUT_KEYS}
    provenance.update({
        "rs_rating": {"status": score.status, "last_bar_date": score.last_bar_date.isoformat()
                      if score.last_bar_date else None},
        "universe_scored": {"universe": UNIVERSE_NAME, "excluded": universe_excluded,
                            "minimum": MIN_SCORED_UNIVERSE},
        "group_rank_pct": {"status": group.status, "groups_ranked": group.groups_ranked},
        "rs_line_pct_below_high_52w": {"status": line.status, "sessions": line.sessions},
        "l_data_integrity": {"reasons": list(dict.fromkeys(diagnostics))},
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
        "l_input_contract": {
            "version": CONTRACT_VERSION,
            "calculation_version": RELATIVE_STRENGTH_VERSION,
            "price_series_version": PRICE_SERIES_VERSION,
            "split_basis_version": SPLIT_BASIS_VERSION,
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": {key: {"value": values[key], **provenance[key]} for key in L_INPUT_KEYS},
        },
        "integrity": {
            "l_data_integrity": integrity,
            "split_integrity_status": split_status,
            "filer_status": filer_status,
            "diagnostics": list(dict.fromkeys(diagnostics)),
        },
    }
