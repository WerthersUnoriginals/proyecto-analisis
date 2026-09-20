"""Pure adapter from current effective fundamentals to the C input contract."""

from typing import Sequence

from database.effective_fundamentals import (
    ComparisonDiagnostic,
    EffectiveObservation,
    GrowthAcceleration,
    annual_comparisons_by_source,
    growth_acceleration_by_source,
    growth_yoy_by_source,
    load_effective_current,
)
from database.normalized_fundamentals import NormalizedObservation


def _effective_observations(effective_rows: Sequence[dict]) -> list[EffectiveObservation]:
    """Convert view rows without supplementing them from any other source.

    The view does not expose ``intrinsic_quality_reasons``. An empty tuple is
    the neutral dataclass value; this field is not used by annual comparison,
    YoY, acceleration, or the already-effective selection represented here.
    """
    result = []
    for row in effective_rows:
        observation = NormalizedObservation(
            company_id=row["company_id"],
            metric=row["metric"],
            source=row["source"],
            source_variant=row["source_variant"],
            observation_kind=row["observation_kind"],
            value=row["value"],
            unit=row["unit"],
            currency=row["currency"],
            source_period_start=row["source_period_start"],
            source_period_end=row["source_period_end"],
            canonical_period_end=row["canonical_period_end"],
            series_date=row["series_date"],
            fiscal_year=row["fiscal_year"],
            fiscal_quarter=row["fiscal_quarter"],
            filed_date=row["filed_date"],
            source_available_at=row["source_available_at"],
            observed_at=row["observed_at"],
            raw_id=row["raw_id"],
            normalizer_version=row["normalizer_version"],
            intrinsic_quality_status=row["intrinsic_quality_status"],
            intrinsic_quality_reasons=(),
            selection_eligibility=row["selection_eligibility"],
            alignment_method=row["alignment_method"],
            alignment_days=row["alignment_days"],
            alignment_reference_id=row["alignment_reference_id"],
            source_metric_name=row["source_metric_name"],
            source_unit=row["source_unit"],
            source_scale_factor=row["source_scale_factor"],
            id=row["selected_observation_id"],
        )
        comparison_reason = row["comparison_reason"]
        diagnostic = ComparisonDiagnostic(
            comparison_status=row["comparison_status"],
            comparison_reasons=(
                () if comparison_reason is None else (comparison_reason,)
            ),
            comparison_reference_id=row["comparison_reference_id"],
            comparison_diff_pct=(
                None
                if row["comparison_difference_pct"] is None
                else float(row["comparison_difference_pct"])
            ),
            comparison_rules_version=row["comparison_rules_version"],
        )
        result.append(EffectiveObservation(
            observation=observation,
            selection_policy_version=row["selection_policy_version"],
            selection_reason=row["selection_reason"],
            comparison=diagnostic,
        ))
    return sorted(result, key=lambda item: (
        item.observation.company_id,
        item.observation.metric,
        item.observation.series_date,
        item.observation.source_variant,
        item.observation.raw_id,
    ))


def _effective_order(item: EffectiveObservation):
    observation = item.observation
    return observation.series_date, observation.source_variant, observation.raw_id


def _latest_observation(
    observations: Sequence[EffectiveObservation], metric: str,
) -> EffectiveObservation | None:
    matching = [item for item in observations if item.observation.metric == metric]
    return max(matching, key=_effective_order) if matching else None


def _latest_acceleration(
    accelerations: Sequence[GrowthAcceleration], metric: str,
) -> GrowthAcceleration | None:
    matching = [item for item in accelerations if item.latest_yoy.metric == metric]
    if not matching:
        return None
    return max(matching, key=lambda item: _effective_order(item.latest_yoy.current))


def _number(value):
    return None if value is None else float(value)


def build_c_fundamental_report(effective_rows: Sequence[dict]) -> dict:
    """Build only the current fundamental inputs consumed by the C model."""
    observations = _effective_observations(effective_rows)
    growth = growth_yoy_by_source(observations)
    accelerations = growth_acceleration_by_source(growth)

    eps_acceleration = _latest_acceleration(accelerations, "EPS_DILUTED")
    revenue_acceleration = _latest_acceleration(accelerations, "REVENUE")
    latest_eps_observation = _latest_observation(observations, "EPS_DILUTED")

    eps_growth = sorted(
        (item for item in growth if item.metric == "EPS_DILUTED"),
        key=lambda item: _effective_order(item.current),
    )
    eps_comparisons = annual_comparisons_by_source(observations, "EPS_DILUTED")
    latest_eps_comparison = next(
        (
            item for item in eps_comparisons
            if latest_eps_observation is not None
            and item.current is latest_eps_observation
        ),
        None,
    )
    eps_loss_to_profit = bool(
        latest_eps_comparison is not None
        and latest_eps_comparison.comparable.observation.value <= 0
        and latest_eps_comparison.current.observation.value > 0
    )

    return {
        "latest_eps_yoy_pct": _number(
            None if eps_acceleration is None else eps_acceleration.latest_yoy.yoy_pct
        ),
        "previous_eps_yoy_pct": _number(
            None
            if eps_acceleration is None or eps_acceleration.previous_yoy is None
            else eps_acceleration.previous_yoy.yoy_pct
        ),
        "eps_acceleration_pp": _number(
            None if eps_acceleration is None else eps_acceleration.acceleration_pp
        ),
        "latest_revenue_yoy_pct": _number(
            None
            if revenue_acceleration is None
            else revenue_acceleration.latest_yoy.yoy_pct
        ),
        "previous_revenue_yoy_pct": _number(
            None
            if revenue_acceleration is None or revenue_acceleration.previous_yoy is None
            else revenue_acceleration.previous_yoy.yoy_pct
        ),
        "revenue_acceleration_pp": _number(
            None
            if revenue_acceleration is None
            else revenue_acceleration.acceleration_pp
        ),
        "latest_eps": _number(
            None
            if latest_eps_observation is None
            else latest_eps_observation.observation.value
        ),
        "eps_yoy_pct": [
            {"date": item.current_series_date.isoformat(), "value": float(item.yoy_pct)}
            for item in eps_growth
        ],
        "eps_loss_to_profit": eps_loss_to_profit,
    }


def load_c_fundamental_report(company_id: int) -> dict:
    """Load the current effective view and adapt it without side effects."""
    return build_c_fundamental_report(load_effective_current(company_id))
