"""M Score v1 experimental (spec docs/superpowers/specs/2026-10-03-market-direction-m-v1-design.md §7).

Weights, thresholds (rule B) and the classic rule approved by the human on
2026-10-04. The score measures how favorable the market direction is for
buying; it is not a probability of a price rise.
"""

from __future__ import annotations

from c_score_v1 import _num, _piecewise, _score_class

MODEL_VERSION = "m-1.0-exp"

STATE_POINTS = {"CONFIRMED_UPTREND": 50.0, "UPTREND_UNDER_PRESSURE": 25.0, "CORRECTION": 0.0}
DISTRIBUTION_KNOTS = [(0.0, 15.0), (2.0, 12.0), (4.0, 6.0), (6.0, 0.0)]
MA_POINTS_EACH = 5.0
BREADTH_KNOTS = [(20.0, 0.0), (40.0, 6.0), (60.0, 12.0), (70.0, 15.0)]
CLASSIC = {"CONFIRMED_UPTREND": "PASS", "UPTREND_UNDER_PRESSURE": "FAIL_UPTREND_UNDER_PRESSURE",
           "CORRECTION": "FAIL_MARKET_IN_CORRECTION"}
MIN_AVAILABLE_POINTS = 80.0


def build_m_score(report: dict) -> dict:
    state = report.get("market_state")
    distribution = [value for value in (report.get("distribution_days") or {}).values() if value is not None]
    ma50 = report.get("pct_vs_ma50") or {}
    ma200 = report.get("pct_vs_ma200") or {}
    ma_values = list(ma50.values()) + list(ma200.values())
    breadth = _num(report.get("breadth_pct_above_ma50"))
    components = {
        "market_state": ({"points": STATE_POINTS[state], "max": 50.0, "available": True} if state in STATE_POINTS
                         else {"points": None, "max": 50.0, "available": False, "status": "NO_VALUE"}),
        "distribution_days": ({"points": _piecewise(max(distribution), DISTRIBUTION_KNOTS), "max": 15.0,
                               "available": True} if distribution
                              else {"points": None, "max": 15.0, "available": False, "status": "NO_VALUE"}),
        "moving_averages": ({"points": sum(MA_POINTS_EACH for value in ma_values if value > 0),
                             "max": MA_POINTS_EACH * len(ma_values), "available": True}
                            if ma_values and all(value is not None for value in ma_values)
                            else {"points": None, "max": 20.0, "available": False, "status": "NO_VALUE"}),
        "breadth": ({"points": _piecewise(breadth, BREADTH_KNOTS), "max": 15.0, "available": True}
                    if breadth is not None else {"points": None, "max": 15.0, "available": False, "status": "NO_VALUE"}),
    }
    raw_points = sum(item["points"] for item in components.values() if item["available"])
    available_points = sum(item["max"] for item in components.values() if item["available"])
    normalized = round(raw_points / available_points * 100.0, 2) if available_points else None

    integrity = str(report.get("m_data_integrity", ""))
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
        "m_classic": {"result": CLASSIC.get(state, "INSUFFICIENT_DATA")},
        "m_score_v1": {
            "model_version": MODEL_VERSION,
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": status,
            "usability": "M_SCORE_REVIEW" if needs_review else "M_SCORE_USABLE",
            "components": components,
        },
        "m_flags": [f"FOLLOW_THROUGH_DAY:{report['last_follow_through_day']}"]
        if report.get("last_follow_through_day") else [],
    }
