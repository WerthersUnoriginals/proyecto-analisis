"""C input contract v3: the eleven C inputs from the v3 quarterly view.

The keys are those of the v2 contract, so ``c_score_v1`` consumes the result
unchanged. The semantics differ deliberately (fundamentals v3 spec, T3–T6):

- every latest/previous input describes the latest effective quarter and its
  immediate predecessor; an older quarter is never substituted;
- each YoY uses one source and one concept; acceleration may combine two
  single-source YoY values and then says so;
- values are on the split basis in force at ``as_of``;
- integrity is reconstructed from v3 evidence and fails closed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping

from database.c_data_integrity import aggregate_data_integrity, classify_data_quality
from database.c_dual_run_contract import INDEPENDENT_C_INPUT_KEYS
from database.quarterly_v3 import (
    SELECTION_POLICY_VERSION,
    Growth,
    QuarterKey,
    QuarterlyView,
    QuarterValue,
)
from database.split_basis import SPLIT_BASIS_VERSION, SplitReconciliation

CONTRACT_VERSION = "c-input-contract-v3-11"
INTEGRITY_VERSION = "c-integrity-v3"
STALE_QUARTER_DAYS = 200
CONSISTENCY_QUARTERS = 12
OK_TOLERANCE_PCT = Decimal("1")
ACCOUNTING_TOLERANCE_PCT = Decimal("5")
EPS_HISTORY_LENGTH = 12

_SHARES_QUALITY = {
    "WeightedAverageNumberOfDilutedSharesOutstanding": "DILUTED_EXACT",
    "WeightedAverageNumberOfShareOutstandingBasicAndDiluted": "BASIC_AND_DILUTED",
}
_HARD_SPLIT_DIAGNOSTICS = {"SPLIT_BASIS_UNCERTAIN"}


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _label(key: QuarterKey | None) -> str | None:
    return None if key is None else f"{key.fiscal_year}Q{key.fiscal_quarter}"


def _value_provenance(value: QuarterValue | None) -> dict:
    if value is None:
        return {}
    return {
        "source": value.source,
        "source_variant": value.source_variant,
        "concept": value.concept,
        "kind": value.kind,
        "fiscal_quarter": _label(value.key),
        "period_start": None if value.period_start is None else value.period_start.isoformat(),
        "period_end": value.period_end.isoformat(),
        "reported_value": str(value.reported_value),
        "basis_factor": str(value.basis_factor),
        "filed_date": None if value.filed_date is None else value.filed_date.isoformat(),
        "accession": value.accession,
        "observed_at": None if value.observed_at is None else value.observed_at.isoformat(),
        "lineage": list(value.lineage),
    }


def _growth_provenance(growth: Growth | None) -> dict:
    if growth is None:
        return {"reasons": ["NO_VALUE"]}
    result = _value_provenance(growth.current)
    result.update({
        "source": growth.source,
        "concept": growth.concept,
        "fiscal_quarter": _label(growth.key),
        "comparable_fiscal_quarter": _label(growth.key.shift(-4)),
        "comparable": _value_provenance(growth.comparable),
        "reasons": list(growth.reasons),
    })
    result["lineage"] = list(growth.current.lineage if growth.current else ()) + list(
        growth.comparable.lineage if growth.comparable else ()
    )
    return result


def growth_status(growth: Growth | None) -> str:
    """Classify a quarter for the score: known growth, known loss, or unknown."""

    if growth is None or growth.current is None:
        return "NO_DATA"
    if growth.yoy_pct is not None:
        return "GROWTH"
    if growth.loss_to_profit:
        return "LOSS_TO_PROFIT"
    if growth.current.value <= 0:
        return "LOSS"
    return "NO_DATA"


def _status_item(growth: Growth | None, key: QuarterKey) -> dict:
    return {
        "quarter": _label(key),
        "status": growth_status(growth),
        "yoy_pct": _number(growth.yoy_pct) if growth else None,
    }


def eps_growth_detail(view: QuarterlyView) -> dict | None:
    """Per-quarter EPS status for the latest five fiscal quarters (score v1.3)."""

    latest_key = view.latest_key("EPS_DILUTED")
    if latest_key is None:
        return None
    latest = view.growth("EPS_DILUTED", latest_key)
    previous = view.growth("EPS_DILUTED", latest_key.shift(-1))
    comparable = latest.comparable
    return {
        "latest": {**_status_item(latest, latest_key),
                   "comparable_eps": _number(comparable.value) if comparable else None},
        "previous": _status_item(previous, latest_key.shift(-1)),
        "quarters": [
            _status_item(view.growth("EPS_DILUTED", latest_key.shift(offset)), latest_key.shift(offset))
            for offset in range(-4, 1)
        ],
    }


def _trend(view: QuarterlyView, metric: str):
    latest = view.latest_growth(metric)
    if latest is None:
        return None, None, None, {"reasons": ["NO_VALUE"]}
    previous = view.growth(metric, latest.key.shift(-1))
    acceleration = None
    reasons = []
    if latest.yoy_pct is not None and previous.yoy_pct is not None:
        acceleration = latest.yoy_pct - previous.yoy_pct
        if latest.source != previous.source:
            reasons.append("MIXED_SOURCE_ACCELERATION")
    else:
        reasons.append("ACCELERATION_UNAVAILABLE")
    acceleration_provenance = {
        "latest": _growth_provenance(latest),
        "previous": _growth_provenance(previous),
        "reasons": reasons,
        "lineage": _growth_provenance(latest)["lineage"] + _growth_provenance(previous)["lineage"],
    }
    return latest, previous, acceleration, acceleration_provenance


def _consistency(view: QuarterlyView, metric: str, latest: QuarterKey | None) -> dict:
    comparisons = [
        item for item in view.source_comparisons(metric)
        if latest is not None and item.key.index > latest.index - CONSISTENCY_QUARTERS
    ]
    if not comparisons:
        return {"status": "N/D", "max_diff_pct": None, "matched_count": 0, "reasons": []}
    maximum = max(item.difference_pct for item in comparisons)
    if maximum <= OK_TOLERANCE_PCT:
        status = "OK"
    elif maximum <= ACCOUNTING_TOLERANCE_PCT:
        status = "DISCREPANCIA_CONTABLE"
    else:
        status = "DISCREPANCIA_ALTA"
    worst = max(comparisons, key=lambda item: item.difference_pct)
    return {
        "status": status,
        "max_diff_pct": float(maximum),
        "matched_count": len(comparisons),
        "reasons": [] if status == "OK" else [f"WORST:{_label(worst.key)}"],
    }


def _split_status(view: QuarterlyView, splits: SplitReconciliation) -> tuple[str, list[str]]:
    reasons = list(splits.reasons) + list(view.split_status_reasons)
    reasons += [item for item in view.diagnostics if item in _HARD_SPLIT_DIAGNOSTICS]
    if view.split_status_reasons or any(item in _HARD_SPLIT_DIAGNOSTICS for item in view.diagnostics):
        return "REVIEW_REQUIRED", reasons
    return splits.status, reasons


def build_c_contract_v3(
    view: QuarterlyView,
    splits: SplitReconciliation,
    *,
    company_id: int,
    as_of: datetime,
) -> dict:
    """Return the eleven C inputs, their provenance, and integrity details."""

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    eps_latest, eps_previous, eps_acceleration, eps_acceleration_provenance = _trend(view, "EPS_DILUTED")
    revenue_latest, revenue_previous, revenue_acceleration, revenue_acceleration_provenance = _trend(view, "REVENUE")

    eps_effective = view.effective("EPS_DILUTED")
    eps_key = view.latest_key("EPS_DILUTED")
    latest_eps_value = eps_effective.get(eps_key) if eps_key else None
    history = view.growth_history("EPS_DILUTED")[-EPS_HISTORY_LENGTH:]

    values = {
        "latest_eps_yoy_pct": _number(eps_latest.yoy_pct if eps_latest else None),
        "previous_eps_yoy_pct": _number(eps_previous.yoy_pct if eps_previous else None),
        "eps_acceleration_pp": _number(eps_acceleration),
        "latest_revenue_yoy_pct": _number(revenue_latest.yoy_pct if revenue_latest else None),
        "previous_revenue_yoy_pct": _number(revenue_previous.yoy_pct if revenue_previous else None),
        "revenue_acceleration_pp": _number(revenue_acceleration),
        "latest_eps": _number(latest_eps_value.value if latest_eps_value else None),
        "eps_yoy_pct": [
            {"date": growth.current.period_end.isoformat(), "value": float(growth.yoy_pct)}
            for growth in history
        ],
        "eps_loss_to_profit": bool(eps_latest and eps_latest.loss_to_profit),
    }
    provenance = {
        "latest_eps_yoy_pct": _growth_provenance(eps_latest),
        "previous_eps_yoy_pct": _growth_provenance(eps_previous),
        "eps_acceleration_pp": eps_acceleration_provenance,
        "latest_revenue_yoy_pct": _growth_provenance(revenue_latest),
        "previous_revenue_yoy_pct": _growth_provenance(revenue_previous),
        "revenue_acceleration_pp": revenue_acceleration_provenance,
        "latest_eps": {**_value_provenance(latest_eps_value), "reasons": [] if latest_eps_value else ["NO_VALUE"]},
        "eps_yoy_pct": {
            "reasons": [],
            "quarters": [_label(growth.key) for growth in history],
            "sources": sorted({growth.source for growth in history}),
            "lineage": [raw for growth in history for raw in _growth_provenance(growth)["lineage"]],
        },
        "eps_loss_to_profit": _growth_provenance(eps_latest),
    }

    split_status, split_reasons = _split_status(view, splits)
    quality = classify_data_quality(
        **{key: values[key] for key in (
            "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
            "latest_revenue_yoy_pct", "previous_revenue_yoy_pct", "revenue_acceleration_pp",
        )},
        current_yoy_crosses_split=False,
        split_integrity_status=split_status,
    )
    consistency = {
        "EPS": _consistency(view, "EPS_DILUTED", eps_key),
        "REVENUE": _consistency(view, "REVENUE", view.latest_key("REVENUE")),
        "NET_INCOME": _consistency(view, "NET_INCOME", view.latest_key("NET_INCOME")),
    }
    shares_key = view.latest_key("DILUTED_SHARES")
    shares = view.effective("DILUTED_SHARES").get(shares_key) if shares_key else None
    shares_quality = _SHARES_QUALITY.get(shares.concept, "REVIEW_REQUIRED") if shares else "NOT_AVAILABLE"
    aggregate = aggregate_data_integrity(quality, split_status, consistency, shares_quality)

    diagnostics = list(aggregate.diagnostics) + list(view.diagnostics) + split_reasons
    data_integrity = aggregate.data_integrity
    if latest_eps_value is None or (as_of.date() - latest_eps_value.period_end).days > STALE_QUARTER_DAYS:
        diagnostics.append("STALE_LATEST_QUARTER" if latest_eps_value else "NO_EPS_EVIDENCE")
        data_integrity = "REVIEW_REQUIRED"
    values["data_integrity"] = data_integrity
    values["split_integrity_status"] = split_status
    provenance["data_integrity"] = {"reasons": list(dict.fromkeys(diagnostics))}
    provenance["split_integrity_status"] = {
        "reasons": list(dict.fromkeys(split_reasons)),
        "events": [
            {"date": event.event_date.isoformat(), "ratio": str(event.ratio), "sources": list(event.sources)}
            for event in splits.events
        ],
    }

    as_of_text = as_of.isoformat()
    inputs = {}
    for key in INDEPENDENT_C_INPUT_KEYS:
        item = dict(provenance[key])
        item.setdefault("reasons", [])
        inputs[key] = {"value": values[key], **item}
    return {
        **values,
        "eps_growth_detail": eps_growth_detail(view),
        "company_id": company_id,
        "as_of": as_of_text,
        "c_input_contract": {
            "version": CONTRACT_VERSION,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "split_basis_version": SPLIT_BASIS_VERSION,
            "integrity_version": INTEGRITY_VERSION,
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": inputs,
        },
        "integrity": {
            "data_integrity": data_integrity,
            "data_quality": quality,
            "split_integrity_status": split_status,
            "consistency": consistency,
            "shares_quality": shares_quality,
            "warnings": list(aggregate.warnings),
            "diagnostics": list(dict.fromkeys(diagnostics)),
        },
    }
