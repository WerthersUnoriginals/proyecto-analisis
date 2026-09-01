"""C Score v1 experimental para CAN SLIM+ v2.6.

El score cuantifica la fortaleza de Current Earnings sin mezclarla con la
integridad del dato. DATA_INTEGRITY sigue siendo un control independiente.
La sorpresa EPS se conserva como componente de 5 puntos, pero queda desactivada
hasta validar su semántica en Yahoo/yfinance; el total se normaliza por los
puntos realmente disponibles.
"""

from __future__ import annotations

import math
from typing import Optional


EPS_GROWTH_KNOTS = [
    (0.0, 0.0), (10.0, 7.0), (15.0, 11.0), (20.0, 15.0),
    (25.0, 20.0), (35.0, 24.0), (50.0, 28.0), (75.0, 32.0), (100.0, 35.0),
]
EPS_ACCEL_KNOTS = [(-30.0, 0.0), (-20.0, 2.0), (-10.0, 4.0), (0.0, 6.0), (10.0, 8.0), (20.0, 10.0)]
SALES_GROWTH_KNOTS = [
    (0.0, 0.0), (5.0, 4.0), (10.0, 8.0), (15.0, 12.0),
    (20.0, 15.0), (25.0, 17.0), (35.0, 19.0), (50.0, 20.0),
]
SALES_ACCEL_KNOTS = [(-15.0, 0.0), (-10.0, 2.0), (-5.0, 4.0), (0.0, 6.0), (5.0, 8.0), (10.0, 10.0)]


def _num(value) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _piecewise(value: Optional[float], knots: list[tuple[float, float]]) -> Optional[float]:
    value = _num(value)
    if value is None:
        return None
    if value <= knots[0][0]:
        return knots[0][1]
    if value >= knots[-1][0]:
        return knots[-1][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x0 <= value <= x1:
            ratio = (value - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    return None


def _recent_values(records: list[dict], n: int = 4) -> list[float]:
    values = []
    for item in records[-n:]:
        value = _num(item.get("value"))
        if value is not None:
            values.append(value)
    return values


def _trend_quality(records: list[dict]) -> Optional[float]:
    values = _recent_values(records, 4)
    if len(values) < 3:
        return None
    deltas = [b - a for a, b in zip(values, values[1:])]
    improving_share = sum(1 for delta in deltas if delta > 0) / len(deltas)
    points = 4.0 * improving_share
    if all(value > 0 for value in values):
        points += 3.0
    if all(value >= 25.0 for value in values):
        points += 3.0
    return min(10.0, points)


def _persistence(records: list[dict]) -> Optional[float]:
    values = _recent_values(records, 4)
    if len(values) < 3:
        return None
    strong_count = sum(1 for value in values if value >= 25.0)
    strong_points = {0: 0.0, 1: 2.0, 2: 4.0, 3: 6.0, 4: 7.0}[strong_count]
    stability = 0.0
    if all(value > 0 for value in values):
        stability += 1.0
    if min(values) >= 10.0:
        stability += 1.0
    if max(values) - min(values) <= 50.0:
        stability += 1.0
    return min(10.0, strong_points + stability)


def _score_class(score: Optional[float]) -> str:
    if score is None:
        return "N/D"
    if score >= 90:
        return "EXCEPTIONAL"
    if score >= 80:
        return "VERY_STRONG"
    if score >= 70:
        return "STRONG"
    if score >= 60:
        return "ACCEPTABLE"
    if score >= 50:
        return "WEAK"
    return "POOR"


def _classic(report: dict) -> dict:
    eps_yoy = _num(report.get("latest_eps_yoy_pct"))
    sales_yoy = _num(report.get("latest_revenue_yoy_pct"))
    eps_accel = _num(report.get("eps_acceleration_pp"))
    sales_accel = _num(report.get("revenue_acceleration_pp"))
    loss_to_profit = bool(report.get("eps_loss_to_profit"))

    eps_25 = bool(loss_to_profit or (eps_yoy is not None and eps_yoy >= 25.0))
    sales_strong = bool(sales_yoy is not None and sales_yoy >= 20.0)
    eps_accelerating = bool(eps_accel is not None and eps_accel > 0)
    sales_accelerating = bool(sales_accel is not None and sales_accel > 0)

    if not eps_25:
        result = "FAIL_EPS_BELOW_25"
    elif eps_accel is not None and eps_accel < 0:
        result = "PASS_WITH_DECELERATION"
    else:
        result = "PASS"

    return {
        "eps_yoy_ge_25": eps_25,
        "sales_growth_strong": sales_strong,
        "eps_accelerating": eps_accelerating,
        "sales_accelerating": sales_accelerating,
        "result": result,
    }


def build_c_score(report: dict) -> dict:
    eps_yoy = _num(report.get("latest_eps_yoy_pct"))
    eps_accel = _num(report.get("eps_acceleration_pp"))
    sales_yoy = _num(report.get("latest_revenue_yoy_pct"))
    sales_accel = _num(report.get("revenue_acceleration_pp"))
    previous_eps_yoy = _num(report.get("previous_eps_yoy_pct"))
    previous_sales_yoy = _num(report.get("previous_revenue_yoy_pct"))

    flags: list[str] = []
    base_effect_risk = bool(
        previous_eps_yoy is not None and previous_eps_yoy < 0
        and eps_yoy is not None and eps_yoy >= 25.0
    )
    if base_effect_risk:
        flags.append("BASE_EFFECT_RISK")
    if report.get("eps_loss_to_profit"):
        flags.append("LOSS_TO_PROFIT")
    if "ACCOUNTING_DIFFERENCE" in str(report.get("data_integrity", "")):
        flags.append("ACCOUNTING_DIFFERENCE")
    if report.get("split_integrity_status") == "VERIFIED_ALREADY_ADJUSTED":
        flags.append("SPLIT_VERIFIED")

    eps_growth = _piecewise(eps_yoy, EPS_GROWTH_KNOTS)
    eps_acceleration = _piecewise(eps_accel, EPS_ACCEL_KNOTS)
    if eps_acceleration is not None and base_effect_risk:
        eps_acceleration = min(eps_acceleration, 5.0)
    eps_trend = _trend_quality(report.get("eps_yoy_pct", []))
    sales_growth = _piecewise(sales_yoy, SALES_GROWTH_KNOTS)
    sales_acceleration = _piecewise(sales_accel, SALES_ACCEL_KNOTS)
    if sales_acceleration is not None and previous_sales_yoy is not None and previous_sales_yoy <= 0:
        sales_acceleration = min(sales_acceleration, 6.0)
    persistence = _persistence(report.get("eps_yoy_pct", []))

    # Pendiente de validar semántica y normalización de earnings_history.
    surprise = None

    components = {
        "eps_growth": {"points": eps_growth, "max": 35.0, "available": eps_growth is not None},
        "eps_acceleration": {"points": eps_acceleration, "max": 10.0, "available": eps_acceleration is not None},
        "eps_trend_quality": {"points": eps_trend, "max": 10.0, "available": eps_trend is not None},
        "sales_growth": {"points": sales_growth, "max": 20.0, "available": sales_growth is not None},
        "sales_acceleration": {"points": sales_acceleration, "max": 10.0, "available": sales_acceleration is not None},
        "persistence": {"points": persistence, "max": 10.0, "available": persistence is not None},
        "eps_surprise": {"points": surprise, "max": 5.0, "available": False, "status": "UNVERIFIED"},
    }

    raw_points = sum(item["points"] for item in components.values() if item.get("available"))
    available_points = sum(item["max"] for item in components.values() if item.get("available"))
    normalized = raw_points / available_points * 100.0 if available_points else None
    if normalized is not None:
        normalized = round(normalized, 2)

    score_status = "OK"
    if report.get("data_integrity") == "REVIEW_REQUIRED":
        score_status = "REVIEW_REQUIRED_DATA"
    elif available_points < 80:
        score_status = "PARTIAL_SCORE"

    diagnostic = []
    if eps_yoy is not None and eps_yoy >= 50:
        diagnostic.append("EPS_GROWTH_STRONG")
    if eps_accel is not None and eps_accel < 0:
        diagnostic.append("EPS_DECELERATING")
    if sales_yoy is not None and sales_yoy >= 20:
        diagnostic.append("SALES_CONFIRM_GROWTH")
    if base_effect_risk:
        diagnostic.append("RECOVERY_OR_BASE_EFFECT")
    if persistence is not None and persistence >= 8:
        diagnostic.append("HIGH_PERSISTENCE")

    return {
        "c_classic": _classic(report),
        "c_score_v1": {
            "raw_points": round(raw_points, 2),
            "available_points": round(available_points, 2),
            "normalized_score": normalized,
            "class": _score_class(normalized),
            "status": score_status,
            "components": components,
            "diagnostic": diagnostic,
            "surprise_policy": "EXCLUDED_UNTIL_VERIFIED",
        },
        "c_flags": flags,
    }
