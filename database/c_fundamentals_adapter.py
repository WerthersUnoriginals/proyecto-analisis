"""Pure adapter from current effective fundamentals to the C input contract."""

from datetime import datetime
from typing import Mapping, Sequence

from database.c_dual_run_contract import INDEPENDENT_C_INPUT_KEYS
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
    return _build_report_from_observations(_effective_observations(effective_rows))


def _build_report_from_observations(observations: Sequence[EffectiveObservation]) -> dict:
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


def _normalized_observations(rows: Sequence[Mapping]) -> list[NormalizedObservation]:
    result = []
    for row in rows:
        result.append(NormalizedObservation(
            company_id=row["company_id"], metric=row["metric"], source=row["source"],
            source_variant=row["source_variant"], observation_kind=row["observation_kind"],
            value=row["value"], unit=row["unit"], currency=row.get("currency"),
            source_period_start=row.get("source_period_start"),
            source_period_end=row["source_period_end"],
            canonical_period_end=row.get("canonical_period_end"),
            series_date=row["series_date"], fiscal_year=row.get("fiscal_year"),
            fiscal_quarter=row.get("fiscal_quarter"), filed_date=row.get("filed_date"),
            source_available_at=row.get("source_available_at"), observed_at=row["observed_at"],
            raw_id=row["raw_id"], normalizer_version=row["normalizer_version"],
            intrinsic_quality_status=row["intrinsic_quality_status"],
            intrinsic_quality_reasons=tuple(row.get("intrinsic_quality_reasons") or ()),
            selection_eligibility=row["selection_eligibility"],
            alignment_method=row["alignment_method"], alignment_days=row.get("alignment_days"),
            alignment_reference_id=row.get("alignment_reference_id"),
            source_metric_name=row.get("source_metric_name"), source_unit=row.get("source_unit"),
            source_scale_factor=row.get("source_scale_factor"), id=row.get("id"),
        ))
    return result


def build_c_fundamental_report_from_normalized(
    normalized_rows: Sequence[Mapping], *, as_of: datetime,
) -> dict:
    """Build the nine financial inputs directly from normalized evidence at ``as_of``."""
    from database.effective_fundamentals import select_effective_observations

    observations = _normalized_observations(normalized_rows)
    selected = select_effective_observations(observations, as_of)
    return _build_report_from_observations(selected)


def normalized_rows_to_effective_rows(
    normalized_rows: Sequence[Mapping], *, as_of: datetime,
) -> list[dict]:
    """Project selected normalized evidence to the adapter's stable row contract."""
    from database.effective_fundamentals import select_effective_observations

    selected = select_effective_observations(_normalized_observations(normalized_rows), as_of)
    source_rows = {row.get("id"): row for row in normalized_rows}
    result = []
    for item in selected:
        observation = item.observation
        comparison = item.comparison
        projected = {
            "company_id": observation.company_id,
            "metric": observation.metric,
            "fiscal_year": observation.fiscal_year,
            "fiscal_quarter": observation.fiscal_quarter,
            "canonical_period_end": observation.canonical_period_end,
            "series_date": observation.series_date,
            "value": observation.value,
            "unit": observation.unit,
            "currency": observation.currency,
            "source": observation.source,
            "source_variant": observation.source_variant,
            "observation_kind": observation.observation_kind,
            "selected_observation_id": observation.id,
            "raw_id": observation.raw_id,
            "source_metric_name": observation.source_metric_name,
            "source_unit": observation.source_unit,
            "source_scale_factor": observation.source_scale_factor,
            "source_period_start": observation.source_period_start,
            "source_period_end": observation.source_period_end,
            "filed_date": observation.filed_date,
            "source_available_at": observation.source_available_at,
            "observed_at": observation.observed_at,
            "normalizer_version": observation.normalizer_version,
            "intrinsic_quality_status": observation.intrinsic_quality_status,
            "selection_eligibility": observation.selection_eligibility,
            "selection_policy_version": item.selection_policy_version,
            "selection_reason": item.selection_reason,
            "comparison_rules_version": comparison.comparison_rules_version,
            "comparison_status": comparison.comparison_status,
            "comparison_reason": (
                None if not comparison.comparison_reasons
                else comparison.comparison_reasons[0]
            ),
            "comparison_reference_id": comparison.comparison_reference_id,
            "comparison_difference_pct": comparison.comparison_diff_pct,
            "alignment_method": observation.alignment_method,
            "alignment_days": observation.alignment_days,
            "alignment_reference_id": observation.alignment_reference_id,
        }
        source_row = source_rows.get(observation.id, {})
        for field in ("source_record_id", "source_identity_type", "source_identity_error"):
            if field in source_row:
                projected[field] = source_row[field]
        result.append(projected)
    return result


def build_independent_c_input_report(
    fundamental_report: Mapping,
    integrity: Mapping,
    *,
    company_id: int,
    as_of: datetime,
    provenance: Mapping[str, Mapping] | None = None,
) -> dict:
    """Compose the complete eleven-input contract without legacy complements.

    ``fundamental_report`` contains the nine persisted fundamental inputs and
    ``integrity`` contains values reconstructed from persisted evidence.  Both
    are copied into one point-in-time contract; callers must not pass values
    obtained from the legacy live snapshot.
    """
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    missing = [key for key in INDEPENDENT_C_INPUT_KEYS[:9] if key not in fundamental_report]
    missing += [key for key in INDEPENDENT_C_INPUT_KEYS[9:] if key not in integrity]
    if missing:
        raise ValueError(f"independent C inputs incomplete: {', '.join(missing)}")
    as_of_text = as_of.isoformat()
    values = {key: fundamental_report[key] for key in INDEPENDENT_C_INPUT_KEYS[:9]}
    values.update({key: integrity[key] for key in INDEPENDENT_C_INPUT_KEYS[9:]})
    input_provenance = {}
    for key in INDEPENDENT_C_INPUT_KEYS:
        item = dict((provenance or {}).get(key, {}))
        item.setdefault(
            "reconstruction_source",
            "database.c_data_integrity"
            if key in INDEPENDENT_C_INPUT_KEYS[9:]
            else "persisted.normalized_fundamentals",
        )
        item.setdefault("status", "RECONSTRUCTED")
        item.update({"as_of": as_of_text, "company_id": company_id,
                     "independently_reconstructed": True})
        input_provenance[key] = item
    result = dict(values)
    result.update({
        "company_id": company_id,
        "as_of": as_of_text,
        "independently_reconstructed_by_new_architecture": True,
        "input_provenance": input_provenance,
        "c_input_contract": {
            "version": "c-input-contract-v2-11",
            "company_id": company_id,
            "as_of": as_of_text,
            "inputs": {
                key: {"value": values[key], **input_provenance[key]}
                for key in INDEPENDENT_C_INPUT_KEYS
            },
        },
    })
    return result


def load_c_fundamental_report(company_id: int) -> dict:
    """Load the current effective view and adapt it without side effects."""
    return build_c_fundamental_report(load_effective_current(company_id))
