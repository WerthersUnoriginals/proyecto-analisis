"""Technical (entry phase) input contract v1 (spec 2026-10-04-entry-phase-technical-v2 §3)."""

from __future__ import annotations

from datetime import datetime

from database.entry_phase_v2 import ENTRY_PHASE_VERSION, EntryPhase
from database.prices_v3 import PRICE_SERIES_VERSION, PriceSeries

CONTRACT_VERSION = "t-input-contract-v1"
ENTRY_SCORE_MIN = 4

T_INPUT_KEYS = (
    "layer1_ok",
    "conditions",
    "score",
    "score_final",
    "setup_type",
    "momentum",
    "values",
    "score_history",
    "sessions_since_score_4",
    "last_bar_date",
    "t_data_integrity",
)


def build_t_contract_v1(result: EntryPhase, series: PriceSeries, *, company_id: int, as_of: datetime) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    diagnostics = list(series.diagnostics) + list(series.review_reasons)
    if result.status != "OK":
        diagnostics.append(result.status)
    if series.review_reasons or result.status == "STALE_PRICES":
        integrity = "REVIEW_REQUIRED"
    elif result.status == "INSUFFICIENT_HISTORY":
        integrity = "VERIFIED_WITH_PARTIAL_CORE_DATA"
    else:
        integrity = "VERIFIED"
    values = {
        "layer1_ok": result.layer1_ok,
        "conditions": dict(result.conditions),
        "score": result.score,
        "score_final": result.score_final,
        "setup_type": result.setup_type,
        "momentum": result.momentum,
        "values": dict(result.values),
        "score_history": list(result.score_history),
        "sessions_since_score_4": result.sessions_since_score_4,
        "last_bar_date": result.last_bar_date.isoformat() if result.last_bar_date else None,
        "t_data_integrity": integrity,
    }
    as_of_text = as_of.isoformat()
    return {
        **values,
        "entry_phase_ready": bool(result.layer1_ok and (result.score_final or 0) >= ENTRY_SCORE_MIN
                                  and integrity != "REVIEW_REQUIRED"),
        "company_id": company_id,
        "as_of": as_of_text,
        "t_input_contract": {
            "version": CONTRACT_VERSION,
            "calculation_version": ENTRY_PHASE_VERSION,
            "price_series_version": PRICE_SERIES_VERSION,
            "reference": "docs/reference/score_fase_entrada_v2.pine",
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": {key: {"value": values[key]} for key in T_INPUT_KEYS},
        },
        "integrity": {"t_data_integrity": integrity, "diagnostics": list(dict.fromkeys(diagnostics))},
    }
