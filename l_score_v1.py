"""L Score v1 experimental (spec docs/superpowers/specs/2026-10-03-leader-laggard-l-v1-design.md §8).

Weights and the classic threshold approved by the human on 2026-10-03. The score
measures how much of a leader the stock is; it is not a probability of a
price rise.
"""

from __future__ import annotations

from c_score_v1 import _num, _piecewise, _score_class

MODEL_VERSION = "l-1.0-exp"

RS_RATING_KNOTS = [(50.0, 0.0), (70.0, 20.0), (80.0, 32.0), (87.0, 42.0), (90.0, 46.0), (95.0, 50.0)]
GROUP_RANK_KNOTS = [(0.0, 0.0), (50.0, 8.0), (80.0, 16.0), (90.0, 20.0)]
RANK_IN_GROUP_KNOTS = [(0.0, 0.0), (50.0, 4.0), (80.0, 8.0), (100.0, 10.0)]
RS_LINE_DISTANCE_KNOTS = [(0.0, 14.0), (5.0, 10.0), (10.0, 5.0), (20.0, 0.0)]
RS_LINE_NEW_HIGH_POINTS = 6.0

CLASSIC_MIN_RS_RATING = 80
MIN_AVAILABLE_POINTS = 80.0


def _curve(value, knots, maximum: float) -> dict:
    value = _num(value)
    if value is None:
        return {"points": None, "max": maximum, "available": False, "status": "NO_VALUE"}
    return {"points": _piecewise(value, knots), "max": maximum, "available": True}


def build_l_score(report: dict) -> dict:
    new_high = report.get("rs_line_new_high_recent")
    components = {
        "rs_rating": _curve(report.get("rs_rating"), RS_RATING_KNOTS, 50.0),
        "group_rank": _curve(report.get("group_rank_pct"), GROUP_RANK_KNOTS, 20.0),
        "rank_in_group": _curve(report.get("rank_in_group_pct"), RANK_IN_GROUP_KNOTS, 10.0),
        "rs_line_distance": _curve(report.get("rs_line_pct_below_high_52w"), RS_LINE_DISTANCE_KNOTS, 14.0),
        "rs_line_new_high": (
            {"points": None, "max": RS_LINE_NEW_HIGH_POINTS, "available": False, "status": "NO_VALUE"}
            if new_high is None else
            {"points": RS_LINE_NEW_HIGH_POINTS if new_high else 0.0, "max": RS_LINE_NEW_HIGH_POINTS,
             "available": True}
        ),
    }
    raw_points = sum(item["points"] for item in components.values() if item["available"])
    available_points = sum(item["max"] for item in components.values() if item["available"])
    normalized = round(raw_points / available_points * 100.0, 2) if available_points else None

    rating = report.get("rs_rating")
    classic = ("INSUFFICIENT_DATA" if rating is None
               else "FAIL_LAGGARD" if rating < CLASSIC_MIN_RS_RATING else "PASS")
    diagnostics = (report.get("integrity") or {}).get("diagnostics", [])
    flags = [flag for flag in ("RS_LINE_HIGH_BEFORE_PRICE",) if flag in diagnostics]
    if new_high:
        flags.append("RS_LINE_NEW_HIGH")

    integrity = str(report.get("l_data_integrity", ""))
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
        "l_classic": {"rs_rating_ge_80": rating is not None and rating >= CLASSIC_MIN_RS_RATING, "result": classic},
        "l_score_v1": {
            "model_version": MODEL_VERSION,
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": status,
            "usability": "L_SCORE_REVIEW" if needs_review else "L_SCORE_USABLE",
            "components": components,
        },
        "l_flags": flags,
    }
