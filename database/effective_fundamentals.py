"""Pure SEC/Yahoo v2 contextual diagnostics; no source selection or persistence."""

from dataclasses import dataclass
from fractions import Fraction

from database.normalized_fundamentals import (
    NormalizedObservation, SEC_NORMALIZER_V2, YAHOO_NORMALIZER_V2,
)

COMPARISON_RULES_VERSION = "sec-yahoo-comparison-v1"


@dataclass(frozen=True)
class ComparisonDiagnostic:
    comparison_status: str
    comparison_reasons: tuple[str, ...]
    comparison_reference_id: int | None
    comparison_diff_pct: float | None
    comparison_rules_version: str


# Exact evidence aliases, scoped to their dataset. No substring inference.
_EPS_EVIDENCE = {
    "sec.company_facts": {
        "EPS_BASIC": {"EarningsPerShareBasic"},
        "EPS_DILUTED": {"EarningsPerShareDiluted"},
    },
    "yahoo.fundamentals_timeseries": {
        "EPS_DILUTED": {"quarterlyDilutedEPS"},
    },
    "yfinance.quarterly_income_stmt": {
        "EPS_BASIC": {"Basic EPS", "BasicEPS"},
        "EPS_DILUTED": {"Diluted EPS", "DilutedEPS"},
    },
}


def compare_observations(
    sec: NormalizedObservation, yahoo: NormalizedObservation,
) -> ComparisonDiagnostic:
    """Diagnose Yahoo against SEC (reference_id=sec.id), without altering either.

    Reason codes are stable within COMPARISON_RULES_VERSION. The current
    contract has no conversion registry: contradictory units or different raw
    multipliers require review, rather than guessing a transformation. Values
    are already normalized and must never be multiplied a second time.
    """
    reasons = []
    if sec.source != "SEC" or yahoo.source != "YAHOO":
        reasons.append("SOURCE_ROLE_MISMATCH")
    if (sec.normalizer_version, yahoo.normalizer_version) != (
        SEC_NORMALIZER_V2, YAHOO_NORMALIZER_V2,
    ):
        reasons.append("NORMALIZER_VERSION_MISMATCH")
    if sec.company_id != yahoo.company_id:
        reasons.append("COMPANY_MISMATCH")
    if sec.metric != yahoo.metric:
        reasons.append("METRIC_MISMATCH")
    if "EPS_UNSPECIFIED" in (sec.metric, yahoo.metric):
        reasons.append("EPS_SEMANTICS_UNSPECIFIED")
    for label, item in (("SEC", sec), ("YAHOO", yahoo)):
        if item.metric in {"EPS_BASIC", "EPS_DILUTED"}:
            aliases = _EPS_EVIDENCE.get(item.source_variant, {}).get(item.metric, set())
            if item.source_metric_name not in aliases:
                reasons.append(f"{label}_EPS_SEMANTICS_UNPROVEN")
    if sec.unit != yahoo.unit:
        reasons.append("UNIT_MISMATCH")
    if any(item.source_unit != item.unit for item in (sec, yahoo)):
        reasons.append("SOURCE_UNIT_INCOMPATIBLE")
    factors = (sec.source_scale_factor, yahoo.source_scale_factor)
    if any(factor is None or not factor.is_finite() or factor <= 0 for factor in factors):
        reasons.append("INVALID_SCALE")
    elif factors[0] != factors[1]:
        reasons.append("SCALE_MISMATCH")
    if sec.currency != yahoo.currency or (
        sec.unit != "shares" and (sec.currency is None or yahoo.currency is None)
    ):
        reasons.append("CURRENCY_INCOMPATIBLE")
    identities = ((sec.fiscal_year, sec.fiscal_quarter), (yahoo.fiscal_year, yahoo.fiscal_quarter))
    if any(None in identity for identity in identities) or identities[0] != identities[1]:
        reasons.append("FISCAL_IDENTITY_INCOMPATIBLE")
    if (
        sec.canonical_period_end is None or yahoo.canonical_period_end is None
        or sec.canonical_period_end != yahoo.canonical_period_end
    ):
        reasons.append("CANONICAL_PERIOD_INCOMPATIBLE")
    if any(item.alignment_method == "UNRESOLVED" for item in (sec, yahoo)):
        reasons.append("ALIGNMENT_UNRESOLVED")
    if sec.source_period_start is not None and yahoo.source_period_start is not None:
        if sec.source_period_start != yahoo.source_period_start:
            reasons.append("PERIOD_START_INCOMPATIBLE")
    finite = sec.value.is_finite() and yahoo.value.is_finite()
    if not finite:
        reasons.append("NONFINITE_VALUE")
    elif (sec.value < 0 < yahoo.value) or (yahoo.value < 0 < sec.value):
        reasons.append("SIGN_INCOMPATIBLE")
    if reasons:
        return ComparisonDiagnostic(
            "REVIEW_REQUIRED", tuple(reasons), sec.id, None, COMPARISON_RULES_VERSION,
        )

    # Rational arithmetic preserves exact Decimal thresholds regardless of the
    # caller's Decimal precision; float conversion happens only at the API edge.
    first, second = Fraction(sec.value), Fraction(yahoo.value)
    denominator = max(abs(first), abs(second))
    pct = abs(first - second) / denominator * 100 if denominator else Fraction(0)
    if pct <= Fraction(1, 10):
        status = "MINOR_DIFFERENCE"
    elif pct < 1:
        status = "DISCREPANCY_RECORDED"
    elif pct <= 5:
        status = "REVIEW_REQUIRED"
    else:
        status = "REVIEW_REQUIRED_HIGH"
    return ComparisonDiagnostic(
        status, (), sec.id, float(pct), COMPARISON_RULES_VERSION,
    )
