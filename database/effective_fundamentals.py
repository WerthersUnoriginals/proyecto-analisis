"""Pure SEC/Yahoo v2 comparison and compatible selection; no persistence."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from typing import Sequence

from database.normalized_fundamentals import (
    NormalizedObservation, SEC_NORMALIZER_V2, YAHOO_NORMALIZER_V2, get_connection,
)

COMPARISON_RULES_VERSION = "sec-yahoo-comparison-v1"
SELECTION_POLICY_VERSION = "c-v2.6-compatible-v1"


_LOAD_EFFECTIVE_CURRENT_SQL = """
    SELECT
        company_id,
        metric,
        fiscal_year,
        fiscal_quarter,
        canonical_period_end,
        series_date,
        value,
        unit,
        currency,
        source,
        source_variant,
        observation_kind,
        selected_observation_id,
        raw_id,
        source_metric_name,
        source_unit,
        source_scale_factor,
        source_period_start,
        source_period_end,
        filed_date,
        source_available_at,
        observed_at,
        normalizer_version,
        intrinsic_quality_status,
        selection_eligibility,
        selection_policy_version,
        selection_reason,
        comparison_rules_version,
        comparison_status,
        comparison_reason,
        comparison_reference_id,
        comparison_difference_pct,
        alignment_method,
        alignment_days,
        alignment_reference_id
    FROM fundamentals_effective_current
    WHERE company_id = %s
    ORDER BY company_id, metric, series_date, source_variant, raw_id
"""


def load_effective_current(company_id: int) -> list[dict]:
    """Read the deployed effective projection without re-running selection."""

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(_LOAD_EFFECTIVE_CURRENT_SQL, (company_id,))
            columns = [description.name for description in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


@dataclass(frozen=True)
class ComparisonDiagnostic:
    comparison_status: str
    comparison_reasons: tuple[str, ...]
    comparison_reference_id: int | None
    comparison_diff_pct: float | None
    comparison_rules_version: str


@dataclass(frozen=True)
class EffectiveObservation:
    observation: NormalizedObservation
    selection_policy_version: str
    selection_reason: str
    comparison: ComparisonDiagnostic


@dataclass(frozen=True)
class AnnualComparison:
    current: EffectiveObservation
    comparable: EffectiveObservation


@dataclass(frozen=True)
class YoYGrowth:
    current: EffectiveObservation
    comparable: EffectiveObservation
    company_id: int
    metric: str
    source: str
    current_series_date: date
    comparable_series_date: date
    current_value: Decimal
    comparable_value: Decimal
    yoy_pct: Decimal


@dataclass(frozen=True)
class GrowthAcceleration:
    latest_yoy: YoYGrowth
    previous_yoy: YoYGrowth | None
    acceleration_pp: Decimal | None


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


def _structural_reasons(sec, yahoo):
    """Shared structural checks, including Yahoo/Yahoo variant compatibility."""
    reasons = []
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
    return reasons


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
    reasons.extend(_structural_reasons(sec, yahoo))
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


def _selection_candidate(item, as_of):
    versions = {"SEC": SEC_NORMALIZER_V2, "YAHOO": YAHOO_NORMALIZER_V2}
    variants = {
        "SEC": {"sec.company_facts"},
        "YAHOO": {"yahoo.fundamentals_timeseries", "yfinance.quarterly_income_stmt"},
    }
    if (
        item.source not in versions
        or item.normalizer_version != versions[item.source]
        or item.source_variant not in variants[item.source]
        or item.observation_kind != "REPORTED"
        or item.selection_eligibility != "ELIGIBLE"
        or item.metric not in {"EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES"}
        or (item.source == "YAHOO" and item.metric == "DILUTED_SHARES")
        or (as_of is not None and item.observed_at > as_of)
    ):
        return False
    return not _structural_reasons(item, item)


def _resolve_source_periods(items):
    """SEC uses spec §9 filing/raw rank; ambiguous Yahoo snapshots are withheld.

    Return ambiguous anchors too: losing their evidence must not silently
    promote a lower-priority source at the same period.
    """
    groups = defaultdict(list)
    for item in items:
        if item not in groups[(item.company_id, item.metric, item.source_variant, item.source_period_end)]:
            groups[(item.company_id, item.metric, item.source_variant, item.source_period_end)].append(item)
    resolved, ambiguous = [], []
    for group in groups.values():
        if any(_structural_reasons(first, second) for first, second in combinations(group, 2)):
            ambiguous.extend(group)
            continue
        if group[0].source == "SEC":
            rank = max((item.filed_date, item.raw_id) for item in group)
            best = [item for item in group if (item.filed_date, item.raw_id) == rank]
        else:
            best = group
        if len(best) == 1:
            resolved.extend(best)
        else:
            ambiguous.extend(group)
    return resolved, ambiguous


def _within_window(first, second):
    return (
        first.company_id == second.company_id and first.metric == second.metric
        and abs((first.series_date - second.series_date).days) <= 35
    )


def _unique_nearest(item, candidates):
    """Select the unique closest compatible counterpart, never break a tie."""
    if not candidates:
        return None
    distance = min(abs((item.series_date - other.series_date).days) for other in candidates)
    best = [other for other in candidates if abs((item.series_date - other.series_date).days) == distance]
    return best[0] if len(best) == 1 else None


def _not_compared(reason):
    return ComparisonDiagnostic("NOT_COMPARED", (reason,), None, None, COMPARISON_RULES_VERSION)


def select_effective_observations(
    observations: Sequence[NormalizedObservation], as_of: datetime | None = None,
) -> list[EffectiveObservation]:
    """Select the four C-compatible metrics, preserving original objects/dates.

    Apply observed_at before ranking. SEC filings use (filed_date, raw_id).
    Yahoo TS precedes yfinance, then SEC precedes Yahoo, within inclusive 35
    days and only with demonstrated structural compatibility. Material ties
    and contradictory lower-priority evidence are withheld, never coerced into
    fallback. EPS_BASIC remains distinct and is outside this diluted-C policy.
    """
    resolved, ambiguous = _resolve_source_periods(
        [item for item in observations if _selection_candidate(item, as_of)]
    )
    sec = [item for item in resolved if item.source == "SEC"]
    ts = [item for item in resolved if item.source_variant == "yahoo.fundamentals_timeseries"]
    yf = [item for item in resolved if item.source_variant == "yfinance.quarterly_income_stmt"]
    yahoo = list(ts)
    for item in yf:
        anchors = ts + [x for x in ambiguous if x.source_variant == "yahoo.fundamentals_timeseries"]
        nearby = [other for other in anchors if _within_window(item, other)]
        # Compatible TS wins; contradictory/ambiguous TS requires withholding
        # the association as well. No lower-priority fallback hides that tie.
        if not nearby:
            yahoo.append(item)

    counterparts = defaultdict(list)
    result = []
    for item in yahoo:
        nearby = [other for other in sec if _within_window(item, other)]
        blocked = any(_within_window(item, other) for other in ambiguous if other.source == "SEC")
        compatible = [other for other in nearby if not _structural_reasons(other, item)]
        best = _unique_nearest(item, compatible)
        if blocked:
            continue
        if best is not None:
            counterparts[best].append(item)
        elif not nearby:
            result.append(EffectiveObservation(
                item, SELECTION_POLICY_VERSION, "YAHOO_FALLBACK_NO_SEC_WITHIN_35D",
                _not_compared("NO_COMPARABLE_SEC_WITHIN_35D"),
            ))
        # Nearby but incompatible or tied: do not invent a fallback.
    for item in sec:
        other = _unique_nearest(item, counterparts[item])
        diagnostic = compare_observations(item, other) if other is not None else _not_compared(
            "AMBIGUOUS_YAHOO_COUNTERPART" if counterparts[item] else "NO_COMPARABLE_YAHOO_WITHIN_35D"
        )
        result.append(EffectiveObservation(
            item, SELECTION_POLICY_VERSION,
            "SEC_REPORTED_PRIORITY" if other is not None else "SEC_REPORTED_NO_COMPARABLE_YAHOO",
            diagnostic,
        ))
    return sorted(result, key=lambda effective: (
        effective.observation.company_id, effective.observation.metric,
        effective.observation.series_date, effective.observation.source_variant,
        effective.observation.raw_id,
    ))


_GROWTH_METRICS = {"EPS_DILUTED", "REVENUE", "NET_INCOME"}


def _growth_input(item):
    observation = item.observation
    return (
        item.selection_policy_version == SELECTION_POLICY_VERSION
        and observation.metric in _GROWTH_METRICS
        and observation.source in {"SEC", "YAHOO"}
        and observation.observation_kind == "REPORTED"
        and observation.normalizer_version == (
            SEC_NORMALIZER_V2 if observation.source == "SEC" else YAHOO_NORMALIZER_V2
        )
    )


def annual_comparisons_by_source(
    observations: Sequence[EffectiveObservation], metric: str,
) -> list[AnnualComparison]:
    """Return unique annual pairs before applying numeric YoY restrictions."""
    eligible = [
        item for item in observations
        if _growth_input(item) and item.observation.metric == metric
    ]
    comparisons = []
    for current in eligible:
        target = date.fromordinal(
            current.observation.series_date.toordinal() - 365
        )
        candidates = [
            item for item in eligible
            if item is not current
            and item.observation.company_id == current.observation.company_id
            and item.observation.source == current.observation.source
            and abs((item.observation.series_date - target).days) <= 45
        ]
        if not candidates:
            continue
        distance = min(
            abs((item.observation.series_date - target).days)
            for item in candidates
        )
        nearest = [
            item for item in candidates
            if abs((item.observation.series_date - target).days) == distance
        ]
        if len(nearest) == 1:
            comparisons.append(AnnualComparison(current, nearest[0]))
    return sorted(comparisons, key=lambda item: (
        item.current.observation.company_id,
        item.current.observation.metric,
        item.current.observation.source,
        item.current.observation.series_date,
        item.current.observation.raw_id,
        item.comparable.observation.raw_id,
    ))


def growth_yoy_by_source(rows: Sequence[EffectiveObservation]) -> list[YoYGrowth]:
    """Calculate legacy-compatible YoY without ever crossing source series."""
    growth = []
    comparisons = [
        comparison
        for metric in _GROWTH_METRICS
        for comparison in annual_comparisons_by_source(rows, metric)
    ]
    for comparison in comparisons:
        current = comparison.current
        comparable = comparison.comparable
        previous = comparable.observation.value
        value = current.observation.value
        if (
            not previous.is_finite() or not value.is_finite()
            or previous <= 0
        ):
            continue
        yoy_pct = (value / previous - Decimal("1")) * Decimal("100")
        growth.append(YoYGrowth(
            current=current,
            comparable=comparable,
            company_id=current.observation.company_id,
            metric=current.observation.metric,
            source=current.observation.source,
            current_series_date=current.observation.series_date,
            comparable_series_date=comparable.observation.series_date,
            current_value=value,
            comparable_value=previous,
            yoy_pct=yoy_pct,
        ))
    return sorted(growth, key=lambda item: (
        item.company_id, item.metric, item.source, item.current_series_date,
        item.current.observation.raw_id, item.comparable.observation.raw_id,
    ))


def growth_acceleration_by_source(rows: Sequence[YoYGrowth]) -> list[GrowthAcceleration]:
    """Return each source/metric's latest YoY and its immediate prior quarter."""
    groups = defaultdict(list)
    for item in rows:
        groups[(item.company_id, item.metric, item.source)].append(item)
    trends = []
    for key, group in groups.items():
        dates = sorted({item.current_series_date for item in group})
        latest_date = dates[-1]
        latest = [item for item in group if item.current_series_date == latest_date]
        if len(latest) != 1:
            continue
        previous = None
        if len(dates) >= 2:
            previous_date = dates[-2]
            candidates = [item for item in group if item.current_series_date == previous_date]
            gap = (latest_date - previous_date).days
            if len(candidates) == 1 and 70 <= gap <= 120:
                previous = candidates[0]
        trends.append(GrowthAcceleration(
            latest_yoy=latest[0],
            previous_yoy=previous,
            acceleration_pp=(
                None if previous is None else latest[0].yoy_pct - previous.yoy_pct
            ),
        ))
    return sorted(trends, key=lambda item: (
        item.latest_yoy.company_id,
        item.latest_yoy.metric,
        item.latest_yoy.source,
    ))
