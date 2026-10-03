"""S Score v1 experimental (spec docs/superpowers/specs/2026-10-03-supply-demand-s-v1-design.md §8).

Weights and classic thresholds approved by the human on 2026-10-03. The score
measures how well supply and demand meet O'Neil's S criteria; it is not a
probability of a price rise. Splits and size are flags only.
"""

from __future__ import annotations

from c_score_v1 import _num, _piecewise, _score_class

MODEL_VERSION = "s-1.0-exp"

SHARES_3Y_KNOTS = [(-10.0, 30.0), (-5.0, 26.0), (0.0, 20.0), (5.0, 10.0), (10.0, 4.0), (20.0, 0.0)]
SHARES_1Y_KNOTS = [(-3.0, 10.0), (0.0, 7.0), (3.0, 3.0), (6.0, 0.0)]
DEBT_LEVEL_KNOTS = [(0.0, 15.0), (0.3, 13.0), (0.6, 9.0), (1.0, 5.0), (2.0, 0.0)]
DEBT_TREND_KNOTS = [(-0.3, 10.0), (0.0, 7.0), (0.3, 2.0), (0.6, 0.0)]
VOLUME_KNOTS = [(0.7, 0.0), (0.9, 8.0), (1.0, 15.0), (1.2, 25.0), (1.5, 32.0), (2.0, 35.0)]

CLASSIC_MIN_VOLUME_RATIO = 1.0
CLASSIC_MAX_DILUTION_3Y_PCT = 5.0
CLASSIC_MAX_DEBT_RISE_3Y = 0.25
MIN_AVAILABLE_POINTS = 80.0


def _curve(value, knots, maximum: float) -> dict:
    value = _num(value)
    if value is None:
        return {"points": None, "max": maximum, "available": False, "status": "NO_VALUE"}
    return {"points": _piecewise(value, knots), "max": maximum, "available": True}


def _classic(report: dict) -> dict:
    ratio = _num(report.get("up_down_volume_ratio_50d"))
    shares_3y = _num(report.get("shares_change_3y_pct"))
    debt_change = _num(report.get("debt_to_equity_change"))
    if ratio is None or shares_3y is None:
        result = "INSUFFICIENT_DATA"
    elif ratio < CLASSIC_MIN_VOLUME_RATIO:
        result = "FAIL_DISTRIBUTION"
    elif shares_3y > CLASSIC_MAX_DILUTION_3Y_PCT:
        result = "FAIL_DILUTION"
    elif debt_change is not None and debt_change > CLASSIC_MAX_DEBT_RISE_3Y:
        result = "FAIL_DEBT_RISING"
    else:
        result = "PASS"
    return {
        "accumulation": ratio is not None and ratio >= CLASSIC_MIN_VOLUME_RATIO,
        "no_dilution_3y": shares_3y is not None and shares_3y <= CLASSIC_MAX_DILUTION_3Y_PCT,
        "debt_not_rising": debt_change is None or debt_change <= CLASSIC_MAX_DEBT_RISE_3Y,
        "result": result,
    }


def build_s_score(report: dict) -> dict:
    components = {
        "shares_3y": _curve(report.get("shares_change_3y_pct"), SHARES_3Y_KNOTS, 30.0),
        "shares_1y": _curve(report.get("shares_change_1y_pct"), SHARES_1Y_KNOTS, 10.0),
        "debt_level": _curve(report.get("debt_to_equity_latest"), DEBT_LEVEL_KNOTS, 15.0),
        "debt_trend": _curve(report.get("debt_to_equity_change"), DEBT_TREND_KNOTS, 10.0),
        "volume_demand": _curve(report.get("up_down_volume_ratio_50d"), VOLUME_KNOTS, 35.0),
    }
    raw_points = sum(item["points"] for item in components.values() if item["available"])
    available_points = sum(item["max"] for item in components.values() if item["available"])
    normalized = round(raw_points / available_points * 100.0, 2) if available_points else None

    flags = []
    shares_3y = _num(report.get("shares_change_3y_pct"))
    if shares_3y is not None and shares_3y < 0:
        flags.append("SHARE_BUYBACK_3Y")
    if shares_3y is not None and shares_3y > CLASSIC_MAX_DILUTION_3Y_PCT:
        flags.append("SHARE_DILUTION_3Y")
    if report.get("split_events"):
        flags.append(f"SPLITS_IN_WINDOW:{len(report['split_events'])}")
    if "NO_DEBT_EVIDENCE" in (report.get("integrity") or {}).get("diagnostics", []):
        flags.append("NO_DEBT_EVIDENCE")

    integrity = str(report.get("s_data_integrity", ""))
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
        "s_classic": _classic(report),
        "s_score_v1": {
            "model_version": MODEL_VERSION,
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": status,
            "usability": "S_SCORE_REVIEW" if needs_review else "S_SCORE_USABLE",
            "components": components,
        },
        "s_flags": flags,
    }
