"""C Score v1.3 experimental (spec docs/superpowers/specs/2026-10-02-c-score-v1-3-design.md).

v1.3 keeps every weight, knot, threshold, class, and flag of v1.2
(``c_score_v1``, imported so they cannot drift) and fixes one defect: a quarter
whose YoY cannot be computed because the company lost money was treated as
*missing*, so its EPS components were dropped and the score was renormalized
over sales. A loss is known, unfavorable evidence:

- current loss -> EPS growth and acceleration score 0 (available);
- loss to profit -> EPS growth stays unavailable (partial score, review);
- trend and persistence use the last five fiscal quarters, drop unknown ones,
  keep four, and count a loss as the worst value.

With four growth quarters and no losses the result equals v1.2.
"""

from __future__ import annotations

from typing import Optional

from c_score_v1 import (
    EPS_ACCEL_KNOTS,
    EPS_GROWTH_KNOTS,
    LOW_PERSISTENCE_MAX_POINTS,
    LOW_PERSISTENCE_SCORE_MIN,
    SALES_ACCEL_KNOTS,
    SALES_GROWTH_KNOTS,
    SMALL_BASE_EPS_MAX,
    SMALL_BASE_YOY_MIN,
    _classic,
    _infer_previous_eps,
    _num,
    _piecewise,
    _score_class,
)

MODEL_VERSION = "1.3-exp"
LOSS = "LOSS"
LOSS_TO_PROFIT = "LOSS_TO_PROFIT"
GROWTH = "GROWTH"
NO_DATA = "NO_DATA"


def _latest_status(report: dict, detail: Optional[dict]) -> str:
    if detail and detail.get("latest"):
        return detail["latest"]["status"]
    if _num(report.get("latest_eps_yoy_pct")) is not None:
        return GROWTH
    if report.get("eps_loss_to_profit"):
        return LOSS_TO_PROFIT
    latest_eps = _num(report.get("latest_eps"))
    return LOSS if latest_eps is not None and latest_eps <= 0 else NO_DATA


def _previous_status(report: dict, detail: Optional[dict]) -> str:
    if detail and detail.get("previous"):
        return detail["previous"]["status"]
    return GROWTH if _num(report.get("previous_eps_yoy_pct")) is not None else NO_DATA


def _window(report: dict, detail: Optional[dict]) -> list[tuple[str, Optional[float]]]:
    """Last four known quarters within the last five (status, yoy)."""

    if detail and detail.get("quarters"):
        known = [
            (item["status"], _num(item.get("yoy_pct")))
            for item in detail["quarters"][-5:]
            if item["status"] != NO_DATA
        ]
        return known[-4:]
    values = [_num(item.get("value")) for item in report.get("eps_yoy_pct", [])[-4:]]
    return [(GROWTH, value) for value in values if value is not None]


def _rank(item: tuple[str, Optional[float]]) -> float:
    status, value = item
    if status == GROWTH:
        return value
    return float("-inf") if status == LOSS else 0.0


def _trend_quality(window) -> Optional[float]:
    if len(window) < 3:
        return None
    deltas = [_rank(b) > _rank(a) for a, b in zip(window, window[1:])]
    points = 4.0 * sum(deltas) / len(deltas)
    if all(status != LOSS and (status == LOSS_TO_PROFIT or value > 0) for status, value in window):
        points += 3.0
    if all(status == GROWTH and value >= 25.0 for status, value in window):
        points += 3.0
    return min(10.0, points)


def _persistence(window) -> Optional[float]:
    if len(window) < 3:
        return None
    strong_count = sum(1 for status, value in window if status == GROWTH and value >= 25.0)
    strong_points = {0: 0.0, 1: 2.0, 2: 4.0, 3: 6.0, 4: 7.0}[strong_count]
    stability = 0.0
    if all(status != LOSS and (status == LOSS_TO_PROFIT or value > 0) for status, value in window):
        stability += 1.0
    if all(status == GROWTH and value >= 10.0 for status, value in window):
        stability += 1.0
    growth_values = [value for status, value in window if status == GROWTH]
    if growth_values and max(growth_values) - min(growth_values) <= 50.0:
        stability += 1.0
    return min(10.0, strong_points + stability)


def build_c_score(report: dict) -> dict:
    detail = report.get("eps_growth_detail")
    latest_status = _latest_status(report, detail)
    previous_status = _previous_status(report, detail)
    eps_yoy = _num(report.get("latest_eps_yoy_pct"))
    eps_accel = _num(report.get("eps_acceleration_pp"))
    sales_yoy = _num(report.get("latest_revenue_yoy_pct"))
    sales_accel = _num(report.get("revenue_acceleration_pp"))
    previous_eps_yoy = _num(report.get("previous_eps_yoy_pct"))
    previous_sales_yoy = _num(report.get("previous_revenue_yoy_pct"))
    comparable_eps = _num((detail or {}).get("latest", {}).get("comparable_eps")) if detail else None
    previous_eps = comparable_eps if comparable_eps is not None else _infer_previous_eps(report.get("latest_eps"), eps_yoy)

    flags: list[str] = []
    base_effect_risk = bool(
        eps_yoy is not None and eps_yoy >= 25.0
        and (
            (previous_eps_yoy is not None and previous_eps_yoy < 0)
            or previous_status in (LOSS, LOSS_TO_PROFIT)
        )
    )
    small_base_risk = bool(
        previous_eps is not None and 0 < previous_eps <= SMALL_BASE_EPS_MAX
        and eps_yoy is not None and eps_yoy >= SMALL_BASE_YOY_MIN
        and not report.get("eps_loss_to_profit")
    )
    if base_effect_risk:
        flags.append("BASE_EFFECT_RISK")
    if small_base_risk:
        flags.append("SMALL_BASE_RISK")
    if report.get("eps_loss_to_profit"):
        flags.append("LOSS_TO_PROFIT")
    if latest_status == LOSS:
        flags.append("CURRENT_LOSS")
    if "ACCOUNTING_DIFFERENCE" in str(report.get("data_integrity", "")):
        flags.append("ACCOUNTING_DIFFERENCE")
    if report.get("split_integrity_status") == "VERIFIED_ALREADY_ADJUSTED":
        flags.append("SPLIT_VERIFIED")

    eps_growth_status = None
    if latest_status == LOSS:
        eps_growth, eps_growth_status = 0.0, LOSS
    else:
        eps_growth = _piecewise(eps_yoy, EPS_GROWTH_KNOTS)
        if eps_growth is None and latest_status == LOSS_TO_PROFIT:
            eps_growth_status = LOSS_TO_PROFIT
    if latest_status == LOSS:
        eps_acceleration = 0.0
    elif previous_status in (LOSS, LOSS_TO_PROFIT):
        eps_acceleration = None
    else:
        eps_acceleration = _piecewise(eps_accel, EPS_ACCEL_KNOTS)
    if eps_acceleration is not None and base_effect_risk:
        eps_acceleration = min(eps_acceleration, 5.0)
    window = _window(report, detail)
    eps_trend = _trend_quality(window)
    sales_growth = _piecewise(sales_yoy, SALES_GROWTH_KNOTS)
    sales_acceleration = _piecewise(sales_accel, SALES_ACCEL_KNOTS)
    if sales_acceleration is not None and previous_sales_yoy is not None and previous_sales_yoy <= 0:
        sales_acceleration = min(sales_acceleration, 6.0)
    persistence = _persistence(window)

    eps_growth_component = {"points": eps_growth, "max": 35.0, "available": eps_growth is not None}
    if eps_growth_status:
        eps_growth_component["status"] = eps_growth_status
    components = {
        "eps_growth": eps_growth_component,
        "eps_acceleration": {"points": eps_acceleration, "max": 10.0, "available": eps_acceleration is not None},
        "eps_trend_quality": {"points": eps_trend, "max": 10.0, "available": eps_trend is not None},
        "sales_growth": {"points": sales_growth, "max": 20.0, "available": sales_growth is not None},
        "sales_acceleration": {"points": sales_acceleration, "max": 10.0, "available": sales_acceleration is not None},
        "persistence": {"points": persistence, "max": 10.0, "available": persistence is not None},
        "eps_surprise": {"points": None, "max": 5.0, "available": False, "status": "UNVERIFIED"},
    }

    raw_points = sum(item["points"] for item in components.values() if item.get("available"))
    available_points = sum(item["max"] for item in components.values() if item.get("available"))
    normalized = round(raw_points / available_points * 100.0, 2) if available_points else None

    low_persistence_high_score = bool(
        normalized is not None and normalized >= LOW_PERSISTENCE_SCORE_MIN
        and persistence is not None and persistence <= LOW_PERSISTENCE_MAX_POINTS
    )
    low_persistence_base_effect = bool(low_persistence_high_score and base_effect_risk)
    if low_persistence_high_score:
        flags.append("LOW_PERSISTENCE_HIGH_SCORE")
    if low_persistence_base_effect:
        flags.append("LOW_PERSISTENCE_BASE_EFFECT")

    data_integrity = str(report.get("data_integrity", ""))
    score_status = "OK"
    if data_integrity == "REVIEW_REQUIRED":
        score_status = "REVIEW_REQUIRED_DATA"
    elif available_points < 80:
        score_status = "PARTIAL_SCORE"
    elif low_persistence_high_score:
        score_status = "REVIEW_LOW_PERSISTENCE"
    needs_review = bool(
        data_integrity == "REVIEW_REQUIRED"
        or "PARTIAL_CORE_DATA" in data_integrity
        or available_points < 80
        or low_persistence_high_score
    )

    diagnostic = []
    if eps_yoy is not None and eps_yoy >= 50:
        diagnostic.append("EPS_GROWTH_STRONG")
    if eps_accel is not None and eps_accel < 0:
        diagnostic.append("EPS_DECELERATING")
    if sales_yoy is not None and sales_yoy >= 20:
        diagnostic.append("SALES_CONFIRM_GROWTH")
    if base_effect_risk:
        diagnostic.append("RECOVERY_OR_BASE_EFFECT")
    if small_base_risk:
        diagnostic.append("EXTREME_GROWTH_FROM_SMALL_EPS_BASE")
    if latest_status == LOSS:
        diagnostic.append("CURRENT_QUARTER_LOSS")
    if persistence is not None and persistence >= 8:
        diagnostic.append("HIGH_PERSISTENCE")
    if low_persistence_high_score:
        diagnostic.append("HIGH_SCORE_WITH_LOW_PERSISTENCE")
    if low_persistence_base_effect:
        diagnostic.append("LOW_PERSISTENCE_BASE_EFFECT_REVIEW")

    return {
        "c_classic": _classic(report),
        "c_score_v1": {
            "model_version": MODEL_VERSION,
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": score_status,
            "usability": "C_SCORE_REVIEW" if needs_review else "C_SCORE_USABLE",
            "components": components,
            "diagnostic": diagnostic,
            "eps_status": {"latest": latest_status, "previous": previous_status, "window": [s for s, _ in window]},
            "surprise_policy": "EXCLUDED_UNTIL_VERIFIED",
            "low_persistence_policy": {
                "review": low_persistence_high_score,
                "score_threshold": LOW_PERSISTENCE_SCORE_MIN,
                "persistence_max": LOW_PERSISTENCE_MAX_POINTS,
                "base_effect": low_persistence_base_effect,
            },
            "small_base": {
                "risk": small_base_risk,
                "comparable_eps": comparable_eps,
                "inferred_previous_eps": None if comparable_eps is not None else previous_eps,
                "eps_threshold": SMALL_BASE_EPS_MAX,
                "yoy_threshold_pct": SMALL_BASE_YOY_MIN,
                "score_penalty": 0.0,
            },
        },
        "c_flags": flags,
    }
