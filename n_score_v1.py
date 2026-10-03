"""N Score v1 experimental (spec docs/superpowers/specs/2026-10-03-new-highs-n-v1-design.md §9).

Weights and thresholds approved by the human on 2026-10-03. The score measures
how close the price is to its highs; it is not a probability of a price rise,
and being at a high is not an entry signal (that is the technical layer).
Catalysts are reported as flags only and never change the score.
"""

from __future__ import annotations

from c_score_v1 import _num, _piecewise, _score_class

MODEL_VERSION = "n-1.0-exp"

DISTANCE_52W_KNOTS = [(0.0, 60.0), (5.0, 55.0), (10.0, 45.0), (15.0, 30.0), (25.0, 10.0), (35.0, 0.0)]
DISTANCE_5Y_KNOTS = [(0.0, 15.0), (10.0, 10.0), (25.0, 4.0), (40.0, 0.0)]
RECENT_HIGH_STEPS = ((20, 25.0), (60, 12.0))  # sessions since the high below the bound -> points

CLASSIC_MAX_BELOW_HIGH_PCT = 15.0
CLASSIC_STATUS_RESULTS = frozenset({"INSUFFICIENT_HISTORY", "STALE_PRICES", "NO_PRICE_EVIDENCE"})
MIN_AVAILABLE_POINTS = 80.0
CATALYST_FLAGS = {"5.02": "CATALYST_MANAGEMENT_CHANGE", "2.01": "CATALYST_ACQUISITION_OR_DISPOSITION"}


def _curve(value, knots, maximum: float) -> dict:
    value = _num(value)
    if value is None:
        return {"points": None, "max": maximum, "available": False, "status": "NO_VALUE"}
    return {"points": _piecewise(value, knots), "max": maximum, "available": True}


def _recent_high(sessions_since) -> dict:
    if sessions_since is None:
        return {"points": None, "max": 25.0, "available": False, "status": "NO_VALUE"}
    points = next((value for bound, value in RECENT_HIGH_STEPS if sessions_since < bound), 0.0)
    return {"points": points, "max": 25.0, "available": True}


def _classic(report: dict) -> dict:
    pct = _num(report.get("pct_below_high_52w"))
    within = pct is not None and pct <= CLASSIC_MAX_BELOW_HIGH_PCT
    status = report.get("n_status")
    if status in CLASSIC_STATUS_RESULTS:
        result = status
    elif not within:
        result = "FAIL_FAR_FROM_HIGH"
    else:
        result = "PASS"
    return {"within_15_pct_of_52w_high": within, "result": result}


def build_n_score(report: dict) -> dict:
    components = {
        "distance_52w": _curve(report.get("pct_below_high_52w"), DISTANCE_52W_KNOTS, 60.0),
        "recent_high": _recent_high(report.get("sessions_since_high_52w")),
        "distance_5y": _curve(report.get("pct_below_high_5y"), DISTANCE_5Y_KNOTS, 15.0),
    }
    raw_points = sum(item["points"] for item in components.values() if item["available"])
    available_points = sum(item["max"] for item in components.values() if item["available"])
    normalized = round(raw_points / available_points * 100.0, 2) if available_points else None

    flags = []
    if report.get("new_high_recent"):
        flags.append("NEW_52W_HIGH_RECENT")
    if report.get("split_integrity_status") == "VERIFIED_ALREADY_ADJUSTED":
        flags.append("SPLIT_VERIFIED")
    counts = (report.get("catalysts") or {}).get("counts", {})
    flags += [flag for item, flag in CATALYST_FLAGS.items() if counts.get(item)]

    integrity = str(report.get("price_data_integrity", ""))
    if integrity == "REVIEW_REQUIRED":
        status = "REVIEW_REQUIRED_DATA"
    elif available_points < MIN_AVAILABLE_POINTS:
        status = "PARTIAL_SCORE"
    else:
        status = "OK"
    needs_review = (
        integrity == "REVIEW_REQUIRED"
        or "PARTIAL_CORE_DATA" in integrity
        or available_points < MIN_AVAILABLE_POINTS
    )
    return {
        "n_classic": _classic(report),
        "n_score_v1": {
            "model_version": MODEL_VERSION,
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": status,
            "usability": "N_SCORE_REVIEW" if needs_review else "N_SCORE_USABLE",
            "components": components,
        },
        "n_flags": flags,
    }
