"""A input contract v1 built from the annual v3 view (spec 2026-10-02-annual-earnings-v3)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from database.annual_v3 import ANNUAL_NORMALIZER_VERSION, AnnualGrowth, AnnualView, Cagr
from database.sec_facts import CATALOG_VERSION
from database.split_basis import SPLIT_BASIS_VERSION, SplitReconciliation

CONTRACT_VERSION = "a-input-contract-v1"
STALE_FISCAL_YEAR_DAYS = 455
CONSISTENCY_YEARS = 3
ROE_FLAG_THRESHOLD_PCT = Decimal("100")

A_INPUT_KEYS = (
    "latest_fiscal_year",
    "latest_annual_eps",
    "annual_eps_yoy_pct",
    "annual_eps_status",
    "eps_cagr_3y_pct",
    "eps_cagr_5y_pct",
    "down_years",
    "loss_years",
    "roe_latest_pct",
    "roe_3y_avg_pct",
    "sales_cagr_3y_pct",
    "latest_sales_yoy_pct",
    "annual_data_integrity",
    "split_integrity_status",
)


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _growth_provenance(growth: AnnualGrowth) -> dict:
    lineage = list(growth.current.lineage if growth.current else ()) + list(
        growth.comparable.lineage if growth.comparable else ()
    )
    return {
        "fiscal_year": growth.fiscal_year,
        "status": growth.status,
        "concept": growth.concept,
        "current_filed": growth.current.filed_date.isoformat() if growth.current else None,
        "comparable_filed": growth.comparable.filed_date.isoformat() if growth.comparable else None,
        "basis_factors": [str(item.basis_factor) for item in (growth.current, growth.comparable) if item],
        "lineage": lineage,
        "reasons": list(growth.reasons),
    }


def _cagr_provenance(cagr: Cagr) -> dict:
    return {
        "fiscal_years": [item.fiscal_year for item in (cagr.start, cagr.end) if item],
        "concept": cagr.concept,
        "values": [str(item.value) for item in (cagr.start, cagr.end) if item],
        "lineage": [raw for item in (cagr.start, cagr.end) if item for raw in item.lineage],
        "reasons": list(cagr.reasons),
    }


def build_a_contract_v1(
    view: AnnualView,
    splits: SplitReconciliation,
    *,
    company_id: int,
    as_of: datetime,
) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    latest = view.latest_year("EPS_DILUTED")
    eps = view.effective("EPS_DILUTED")
    diagnostics: list[str] = list(view.diagnostics)

    recent_years = [] if latest is None else list(range(latest - CONSISTENCY_YEARS + 1, latest + 1))
    growths = [view.growth("EPS_DILUTED", year) for year in recent_years]
    down_years = sum(1 for growth in growths if growth.yoy_pct is not None and growth.yoy_pct < 0)
    loss_years = sum(1 for year in recent_years if year in eps and eps[year].value <= 0)
    cagr_3y = view.cagr("EPS_DILUTED", 3)
    cagr_5y = view.cagr("EPS_DILUTED", 5)
    sales_cagr = view.cagr("REVENUE", 3)
    sales_latest = view.latest_year("REVENUE")
    sales_growth = view.growth("REVENUE", sales_latest) if sales_latest else None

    roe = view.roe(latest) if latest else None
    roes = [view.roe(year) for year in recent_years]
    roe_avg = (
        sum(item.value_pct for item in roes) / len(roes)
        if roes and all(item.status == "OK" for item in roes) else None
    )
    if roe is not None and roe.status == "NOT_MEANINGFUL":
        diagnostics.append("ROE_NOT_MEANINGFUL")
    if roe is not None and roe.value_pct is not None and roe.value_pct > ROE_FLAG_THRESHOLD_PCT:
        diagnostics.append("ROE_ABOVE_100_PCT")

    split_status = splits.status
    split_reasons = list(splits.reasons) + list(view.split_status_reasons)
    if view.split_status_reasons or "SPLIT_BASIS_UNCERTAIN" in view.diagnostics:
        split_status = "REVIEW_REQUIRED"

    core_complete = (
        latest is not None
        and cagr_3y.value_pct is not None
        and len(growths) == CONSISTENCY_YEARS
        and all(growth.status != "NO_DATA" for growth in growths)
        and roe is not None and roe.status == "OK"
    )
    if split_status in {"REVIEW_REQUIRED", "UNKNOWN", "UNADJUSTED_DETECTED"}:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append(f"SPLIT_STATUS:{split_status}")
    elif latest is None:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append("NO_ANNUAL_EPS_EVIDENCE")
    elif (as_of.date() - eps[latest].period_end).days > STALE_FISCAL_YEAR_DAYS:
        integrity = "REVIEW_REQUIRED"
        diagnostics.append("STALE_LATEST_FISCAL_YEAR")
    elif roe is not None and roe.status == "NOT_MEANINGFUL":
        integrity = "REVIEW_REQUIRED"
    elif core_complete:
        integrity = "VERIFIED"
    else:
        integrity = "VERIFIED_WITH_PARTIAL_CORE_DATA"

    values = {
        "latest_fiscal_year": latest,
        "latest_annual_eps": _number(eps[latest].value) if latest else None,
        "annual_eps_yoy_pct": [_number(growth.yoy_pct) for growth in growths],
        "annual_eps_status": [growth.status for growth in growths],
        "eps_cagr_3y_pct": _number(cagr_3y.value_pct),
        "eps_cagr_5y_pct": _number(cagr_5y.value_pct),
        "down_years": down_years,
        "loss_years": loss_years,
        "roe_latest_pct": _number(roe.value_pct) if roe else None,
        "roe_3y_avg_pct": _number(roe_avg),
        "sales_cagr_3y_pct": _number(sales_cagr.value_pct),
        "latest_sales_yoy_pct": _number(sales_growth.yoy_pct) if sales_growth else None,
        "annual_data_integrity": integrity,
        "split_integrity_status": split_status,
    }
    roe_provenance = {"reasons": ["NO_ANNUAL_EPS_EVIDENCE"]} if roe is None else {
        "fiscal_year": roe.fiscal_year,
        "status": roe.status,
        "concepts": list(roe.concepts) if roe.concepts else None,
        "net_income": str(roe.net_income.value) if roe.net_income else None,
        "opening_equity": str(roe.opening.value) if roe.opening else None,
        "closing_equity": str(roe.closing.value) if roe.closing else None,
        "lineage": [raw for item in (roe.net_income, roe.opening, roe.closing) if item for raw in item.lineage],
        "reasons": list(roe.reasons),
    }
    provenance = {
        "latest_fiscal_year": {"reasons": [] if latest else ["NO_ANNUAL_EPS_EVIDENCE"]},
        "latest_annual_eps": {} if latest is None else {
            "concept": eps[latest].concept, "reported_value": str(eps[latest].reported_value),
            "basis_factor": str(eps[latest].basis_factor), "filed_date": eps[latest].filed_date.isoformat(),
            "accession": eps[latest].accession, "lineage": list(eps[latest].lineage),
        },
        "annual_eps_yoy_pct": {"years": [_growth_provenance(growth) for growth in growths]},
        "annual_eps_status": {"fiscal_years": recent_years},
        "eps_cagr_3y_pct": _cagr_provenance(cagr_3y),
        "eps_cagr_5y_pct": _cagr_provenance(cagr_5y),
        "down_years": {"fiscal_years": recent_years},
        "loss_years": {"fiscal_years": recent_years},
        "roe_latest_pct": roe_provenance,
        "roe_3y_avg_pct": {"fiscal_years": recent_years, "statuses": [item.status for item in roes]},
        "sales_cagr_3y_pct": _cagr_provenance(sales_cagr),
        "latest_sales_yoy_pct": _growth_provenance(sales_growth) if sales_growth else {"reasons": ["NO_VALUE"]},
        "annual_data_integrity": {"reasons": list(dict.fromkeys(diagnostics))},
        "split_integrity_status": {
            "reasons": list(dict.fromkeys(split_reasons)),
            "events": [
                {"date": event.event_date.isoformat(), "ratio": str(event.ratio), "sources": list(event.sources)}
                for event in splits.events
            ],
        },
    }
    as_of_text = as_of.isoformat()
    return {
        **values,
        "company_id": company_id,
        "as_of": as_of_text,
        "a_input_contract": {
            "version": CONTRACT_VERSION,
            "normalizer_version": ANNUAL_NORMALIZER_VERSION,
            "split_basis_version": SPLIT_BASIS_VERSION,
            "catalog_version": CATALOG_VERSION,
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": {key: {"value": values[key], **provenance[key]} for key in A_INPUT_KEYS},
        },
        "integrity": {
            "annual_data_integrity": integrity,
            "split_integrity_status": split_status,
            "diagnostics": list(dict.fromkeys(diagnostics)),
        },
    }
