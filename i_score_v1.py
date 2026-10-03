"""I Score v1 experimental (spec docs/superpowers/specs/2026-10-03-institutional-i-v1-design.md §8).

Weights and the classic rule approved by the human on 2026-10-03, with a 1%
tolerance on quarter-on-quarter declines (noise such as AAPL −0.02%). The score
measures institutional sponsorship; it is not a probability of a price rise.
"""

from __future__ import annotations

from c_score_v1 import _num, _piecewise, _score_class

MODEL_VERSION = "i-1.0-exp"

HOLDERS_QOQ_KNOTS = [(-5.0, 0.0), (0.0, 12.0), (2.0, 20.0), (5.0, 26.0), (10.0, 30.0)]
HOLDERS_YOY_KNOTS = [(-10.0, 0.0), (0.0, 10.0), (5.0, 17.0), (10.0, 22.0), (20.0, 25.0)]
QUARTERS_INCREASING_POINTS = {0: 0.0, 1: 5.0, 2: 9.0, 3: 12.0, 4: 15.0}
OWNERSHIP_KNOTS = [(0.0, 0.0), (20.0, 10.0), (40.0, 22.0), (50.0, 30.0), (85.0, 30.0), (95.0, 24.0), (110.0, 12.0)]
CLASSIC_MAX_QOQ_DECLINE_PCT = -1.0
MIN_AVAILABLE_POINTS = 80.0


def _curve(value, knots, maximum: float) -> dict:
    value = _num(value)
    if value is None:
        return {"points": None, "max": maximum, "available": False, "status": "NO_VALUE"}
    return {"points": _piecewise(value, knots), "max": maximum, "available": True}


def build_i_score(report: dict) -> dict:
    increasing = report.get("quarters_increasing")
    components = {
        "holders_qoq": _curve(report.get("holders_change_qoq_pct"), HOLDERS_QOQ_KNOTS, 30.0),
        "holders_yoy": _curve(report.get("holders_change_yoy_pct"), HOLDERS_YOY_KNOTS, 25.0),
        "quarters_increasing": (
            {"points": None, "max": 15.0, "available": False, "status": "NO_VALUE"} if increasing is None
            else {"points": QUARTERS_INCREASING_POINTS[min(int(increasing), 4)], "max": 15.0, "available": True}
        ),
        "ownership": _curve(report.get("institutional_ownership_pct"), OWNERSHIP_KNOTS, 30.0),
    }
    raw_points = sum(item["points"] for item in components.values() if item["available"])
    available_points = sum(item["max"] for item in components.values() if item["available"])
    normalized = round(raw_points / available_points * 100.0, 2) if available_points else None

    qoq, yoy = _num(report.get("holders_change_qoq_pct")), _num(report.get("holders_change_yoy_pct"))
    if qoq is None or yoy is None:
        classic = "INSUFFICIENT_DATA"
    elif qoq < CLASSIC_MAX_QOQ_DECLINE_PCT:
        classic = "FAIL_DECLINING_SPONSORSHIP"
    else:
        classic = "PASS"
    diagnostics = (report.get("integrity") or {}).get("diagnostics", [])
    flags = [flag for flag in ("CUSIP_FROM_NAME_MATCH", "OWNERSHIP_ABOVE_100_PCT") if flag in diagnostics]
    if increasing is not None and increasing >= 2:
        flags.append("SPONSORSHIP_RISING")

    integrity = str(report.get("i_data_integrity", ""))
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
        "i_classic": {"holders_not_declining": qoq is not None and qoq >= CLASSIC_MAX_QOQ_DECLINE_PCT,
                      "result": classic},
        "i_score_v1": {
            "model_version": MODEL_VERSION,
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": status,
            "usability": "I_SCORE_REVIEW" if needs_review else "I_SCORE_USABLE",
            "components": components,
        },
        "i_flags": flags,
    }
