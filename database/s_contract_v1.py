"""S input contract v1 (spec 2026-10-03-supply-demand-s-v1 §5–§6)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Sequence

from database.annual_v3 import ANNUAL_NORMALIZER_VERSION, AnnualView
from database.prices_v3 import PRICE_SERIES_VERSION, PriceSeries
from database.registrant_v3 import registrant_history
from database.sec_facts import CATALOG_VERSION
from database.split_basis import SPLIT_BASIS_VERSION, SplitEvent, SplitReconciliation, split_events_provenance
from database.supply_demand_v1 import SUPPLY_DEMAND_VERSION, Leverage, ShareSupply, VolumeDemand

CONTRACT_VERSION = "s-input-contract-v1"
STALE_FISCAL_YEAR_DAYS = 455
REVIEW_SPLIT_STATUSES = frozenset({"REVIEW_REQUIRED", "UNKNOWN", "UNADJUSTED_DETECTED"})

S_INPUT_KEYS = (
    "latest_fiscal_year",
    "diluted_shares_latest",
    "shares_change_1y_pct",
    "shares_change_3y_pct",
    "debt_to_equity_latest",
    "debt_to_equity_3y_ago",
    "debt_to_equity_change",
    "debt_definition",
    "up_down_volume_ratio_50d",
    "volume_sessions",
    "split_events",
    "market_value_approx",
    "s_data_integrity",
    "split_integrity_status",
)


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def build_s_contract_v1(
    view: AnnualView,
    supply: ShareSupply,
    leverage: Leverage,
    demand: VolumeDemand,
    series: PriceSeries,
    splits: SplitReconciliation,
    *,
    split_status: str,
    split_reasons: Sequence[str],
    rejected_splits: Sequence[SplitEvent],
    company_id: int,
    as_of: datetime,
    filer_status: str = "DOMESTIC",
    registrant_links: tuple = (),
) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    diagnostics: list[str] = list(view.diagnostics) + list(series.diagnostics) + list(series.review_reasons)
    if leverage.status == "NOT_MEANINGFUL":
        diagnostics.append("DEBT_TO_EQUITY_NOT_MEANINGFUL")
    if leverage.status == "NO_DEBT_EVIDENCE":
        diagnostics.append("NO_DEBT_EVIDENCE")
    if leverage.fiscal_year is not None and supply.latest_fiscal_year is not None \
            and leverage.fiscal_year < supply.latest_fiscal_year:
        diagnostics.append("LEVERAGE_NOT_LATEST_FISCAL_YEAR")
    if demand.status != "OK":
        diagnostics.append(demand.status)

    latest_end = view.year_ends.get(supply.latest_fiscal_year) if supply.latest_fiscal_year else None
    history, history_diagnostics, history_review = registrant_history(registrant_links, as_of)
    diagnostics.extend(history_diagnostics)
    core = (supply.change_3y_pct, leverage.debt_to_equity_latest, demand.up_down_volume_ratio_50d)
    if filer_status != "DOMESTIC":
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(filer_status)
    elif history_review:
        integrity = "REVIEW_REQUIRED"
    elif split_status in REVIEW_SPLIT_STATUSES:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(f"SPLIT_STATUS:{split_status}")
    elif supply.latest_fiscal_year is None:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append("NO_SHARE_COUNT_EVIDENCE")
    elif latest_end is not None and (as_of.date() - latest_end).days > STALE_FISCAL_YEAR_DAYS:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append("STALE_LATEST_FISCAL_YEAR")
    elif leverage.status == "NOT_MEANINGFUL" or series.review_reasons or demand.status == "STALE_PRICES":
        integrity = "REVIEW_REQUIRED"
    elif any(value is None for value in core):
        integrity = "VERIFIED_WITH_PARTIAL_CORE_DATA"
    else:
        integrity = "VERIFIED"

    last = series.bars[-1] if series.bars else None
    market_value = (
        last.close * supply.diluted_shares_latest if last and supply.diluted_shares_latest is not None else None
    )
    applied = [event for event in splits.events if event not in rejected_splits]
    values = {
        "latest_fiscal_year": supply.latest_fiscal_year,
        "diluted_shares_latest": _number(supply.diluted_shares_latest),
        "shares_change_1y_pct": _number(supply.change_1y_pct),
        "shares_change_3y_pct": _number(supply.change_3y_pct),
        "debt_to_equity_latest": _number(leverage.debt_to_equity_latest),
        "debt_to_equity_3y_ago": _number(leverage.debt_to_equity_3y_ago),
        "debt_to_equity_change": _number(leverage.change_3y),
        "debt_definition": leverage.definition,
        "up_down_volume_ratio_50d": _number(demand.up_down_volume_ratio_50d),
        "volume_sessions": demand.sessions,
        "split_events": [{"date": event.event_date.isoformat(), "ratio": str(event.ratio.normalize())}
                         for event in applied],
        "market_value_approx": _number(market_value),
        "s_data_integrity": integrity,
        "split_integrity_status": split_status,
    }
    leverage_provenance = {
        "status": leverage.status, "fiscal_year": leverage.fiscal_year, "equity_concept": leverage.equity_concept,
        "debt": str(leverage.debt_latest) if leverage.debt_latest is not None else None,
        "equity": str(leverage.equity_latest) if leverage.equity_latest is not None else None,
        "reasons": list(leverage.reasons),
    }
    provenance = {key: {} for key in S_INPUT_KEYS}
    provenance.update({
        "diluted_shares_latest": {"concept": supply.concept, "lineage": list(supply.lineage)},
        "shares_change_1y_pct": {"reasons": list(supply.reasons_1y)},
        "shares_change_3y_pct": {"reasons": list(supply.reasons_3y)},
        "debt_to_equity_latest": leverage_provenance,
        "debt_to_equity_3y_ago": {"reasons": list(leverage.reasons)},
        "debt_to_equity_change": {"reasons": list(leverage.reasons)},
        "up_down_volume_ratio_50d": {
            "status": demand.status,
            "up_volume": str(demand.up_volume) if demand.up_volume is not None else None,
            "down_volume": str(demand.down_volume) if demand.down_volume is not None else None,
            "reasons": list(demand.reasons),
        },
        "market_value_approx": {"basis": "last close x latest weighted diluted shares (approximate)"},
        "s_data_integrity": {"reasons": list(dict.fromkeys(diagnostics))},
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
        "s_input_contract": {
            "version": CONTRACT_VERSION,
            "calculation_version": SUPPLY_DEMAND_VERSION,
            "normalizer_version": ANNUAL_NORMALIZER_VERSION,
            "price_series_version": PRICE_SERIES_VERSION,
            "split_basis_version": SPLIT_BASIS_VERSION,
            "catalog_version": CATALOG_VERSION,
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": {key: {"value": values[key], **provenance[key]} for key in S_INPUT_KEYS},
        },
        "integrity": {
            "s_data_integrity": integrity,
            "split_integrity_status": split_status,
            "filer_status": filer_status,
            "registrant_history": history,
            "diagnostics": list(dict.fromkeys(diagnostics)),
        },
    }
