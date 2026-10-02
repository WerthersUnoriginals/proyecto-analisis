"""A Score v1 experimental (spec docs/superpowers/specs/2026-10-02-annual-earnings-v3-design.md §8).

Weights and thresholds approved by the human on 2026-10-02. The score measures
how well annual earnings meet the A criteria; it is not a probability of a
price rise. Conventions follow C v1.3: a loss is unfavorable evidence (0
points, available); a value that cannot be expressed (loss to profit, ROE
with non-positive equity) is unavailable and sends the score to review.
"""

from __future__ import annotations

from typing import Optional

from c_score_v1 import _num, _piecewise, _score_class

MODEL_VERSION = "a-1.0-exp"

EPS_CAGR_KNOTS = [(0.0, 0.0), (10.0, 10.0), (15.0, 15.0), (20.0, 20.0), (25.0, 25.0), (35.0, 28.0), (50.0, 30.0)]
CONSISTENCY_YEAR_KNOTS = [(0.0, 0.0), (10.0, 3.0), (20.0, 5.5), (25.0, 20.0 / 3.0)]
ROE_KNOTS = [(0.0, 0.0), (10.0, 8.0), (15.0, 18.0), (17.0, 21.0), (20.0, 23.0), (25.0, 25.0)]
SALES_CAGR_KNOTS = [(0.0, 0.0), (5.0, 5.0), (10.0, 10.0), (15.0, 13.0), (20.0, 15.0)]
STABILITY_POINTS = {0: 10.0, 1: 4.0}

CLASSIC_EPS_GROWTH_MIN = 25.0
CLASSIC_ROE_MIN = 17.0
ROE_FLAG_THRESHOLD = 100.0
MIN_AVAILABLE_POINTS = 80.0


def _input(report: dict, key: str) -> dict:
    return report.get("a_input_contract", {}).get("inputs", {}).get(key, {})


def _cagr_component(report: dict, key: str, knots, maximum: float) -> dict:
    value = _num(report.get(key))
    if value is not None:
        return {"points": _piecewise(value, knots), "max": maximum, "available": True}
    provenance = _input(report, key)
    if "NON_POSITIVE_ENDPOINT" in provenance.get("reasons", []):
        start, end = (_num(item) for item in provenance.get("values", [None, None]))
        if end is not None and end <= 0:
            return {"points": 0.0, "max": maximum, "available": True, "status": "LOSS"}
        return {"points": None, "max": maximum, "available": False, "status": "LOSS_TO_PROFIT"}
    reasons = provenance.get("reasons") or ["NO_VALUE"]
    return {"points": None, "max": maximum, "available": False, "status": reasons[0]}


def _consistency(statuses: list[str], yoys: list[Optional[float]]) -> dict:
    if len(statuses) != 3 or any(status in ("NO_DATA", "LOSS_TO_PROFIT") for status in statuses):
        return {"points": None, "max": 20.0, "available": False,
                "status": "LOSS_TO_PROFIT" if "LOSS_TO_PROFIT" in statuses else "NO_DATA"}
    points = sum(
        0.0 if status == "LOSS" else _piecewise(yoy, CONSISTENCY_YEAR_KNOTS)
        for status, yoy in zip(statuses, yoys)
    )
    return {"points": min(20.0, points), "max": 20.0, "available": True}


def _stability(report: dict) -> dict:
    statuses = report.get("annual_eps_status") or []
    if len(statuses) != 3 or "NO_DATA" in statuses:
        return {"points": None, "max": 10.0, "available": False, "status": "NO_DATA"}
    if report.get("loss_years"):
        return {"points": 0.0, "max": 10.0, "available": True, "status": "LOSS_YEAR"}
    return {"points": STABILITY_POINTS.get(report.get("down_years", 0), 0.0), "max": 10.0, "available": True}


def _roe(report: dict) -> dict:
    value = _num(report.get("roe_latest_pct"))
    if value is None:
        status = _input(report, "roe_latest_pct").get("status") or "NO_DATA"
        return {"points": None, "max": 25.0, "available": False, "status": status}
    return {"points": _piecewise(value, ROE_KNOTS), "max": 25.0, "available": True}


def _classic(report: dict) -> dict:
    statuses = report.get("annual_eps_status") or []
    yoys = [_num(value) for value in report.get("annual_eps_yoy_pct") or []]
    roe = _num(report.get("roe_latest_pct"))
    eps_25 = len(statuses) == 3 and all(
        status == "GROWTH" and yoy is not None and yoy >= CLASSIC_EPS_GROWTH_MIN
        for status, yoy in zip(statuses, yoys)
    )
    roe_17 = roe is not None and roe >= CLASSIC_ROE_MIN
    if len(statuses) != 3 or "NO_DATA" in statuses:
        result = "INSUFFICIENT_HISTORY"
    elif report.get("loss_years"):
        result = "FAIL_LOSS_YEAR"
    elif not eps_25:
        result = "FAIL_EPS_GROWTH"
    elif not roe_17:
        result = "FAIL_ROE"
    else:
        result = "PASS"
    return {
        "eps_growth_ge_25_each_year": eps_25,
        "roe_ge_17": roe_17,
        "no_loss_years": not report.get("loss_years"),
        "result": result,
    }


def build_a_score(report: dict) -> dict:
    statuses = list(report.get("annual_eps_status") or [])
    yoys = [_num(value) for value in report.get("annual_eps_yoy_pct") or []]
    components = {
        "eps_cagr_3y": _cagr_component(report, "eps_cagr_3y_pct", EPS_CAGR_KNOTS, 30.0),
        "eps_consistency": _consistency(statuses, yoys),
        "eps_stability": _stability(report),
        "roe": _roe(report),
        "sales_cagr_3y": _cagr_component(report, "sales_cagr_3y_pct", SALES_CAGR_KNOTS, 15.0),
    }
    raw_points = sum(item["points"] for item in components.values() if item["available"])
    available_points = sum(item["max"] for item in components.values() if item["available"])
    normalized = round(raw_points / available_points * 100.0, 2) if available_points else None

    flags = []
    roe = _num(report.get("roe_latest_pct"))
    if components["roe"].get("status") == "NOT_MEANINGFUL":
        flags.append("ROE_NOT_MEANINGFUL")
    if roe is not None and roe > ROE_FLAG_THRESHOLD:
        flags.append("ROE_ABOVE_100_PCT")
    if "LOSS_TO_PROFIT" in statuses or components["eps_cagr_3y"].get("status") == "LOSS_TO_PROFIT":
        flags.append("LOSS_TO_PROFIT")
    if report.get("loss_years"):
        flags.append("LOSS_YEAR")
    if report.get("down_years"):
        flags.append("DOWN_YEAR")
    if report.get("split_integrity_status") == "VERIFIED_ALREADY_ADJUSTED":
        flags.append("SPLIT_VERIFIED")

    integrity = str(report.get("annual_data_integrity", ""))
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
        "a_classic": _classic(report),
        "a_score_v1": {
            "model_version": MODEL_VERSION,
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": status,
            "usability": "A_SCORE_REVIEW" if needs_review else "A_SCORE_USABLE",
            "components": components,
        },
        "a_flags": flags,
    }
