"""Pure replay of legacy split-integrity semantics over persisted evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Mapping, Sequence


SPLIT_INTEGRITY_TOLERANCE_PCT = 5.0
VALID_SPLIT_INTEGRITY_STATUSES = frozenset({
    "UNKNOWN",
    "NO_RECENT_SPLITS",
    "VERIFIED_ALREADY_ADJUSTED",
    "UNADJUSTED_DETECTED",
    "REVIEW_REQUIRED",
})
SEC_CANDIDATE_TAGS = {
    "EPS_DILUTED": ("EarningsPerShareDiluted",),
    "REVENUE": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "NET_INCOME": ("NetIncomeLoss", "ProfitLoss"),
    "DILUTED_SHARES": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
    ),
}


@dataclass(frozen=True)
class ResolvedSecMetric:
    metric: str
    selected_tag: str | None
    values: Mapping[date, Decimal]
    raw_ids: Mapping[date, int]
    shares_quality: str
    candidate_order_demonstrated: bool
    ambiguous_periods: tuple[date, ...] = ()


@dataclass(frozen=True)
class SplitIntegrityResult:
    status: str
    events: tuple[dict, ...]


def _shares_quality(tag: str | None) -> str:
    if not tag:
        return "NOT_AVAILABLE"
    text = tag.lower()
    if "basicanddiluted" in text or ("basic" in text and "diluted" in text):
        return "BASIC_AND_DILUTED"
    if "diluted" in text:
        return "DILUTED_EXACT"
    if "basic" in text:
        return "BASIC_FALLBACK"
    return "REVIEW_REQUIRED"


def resolve_legacy_sec_metric(
    observations: Iterable[Mapping[str, object]],
    metric: str,
    as_of: datetime,
    window_start: date,
    *,
    candidate_tags: Sequence[str] | None = None,
) -> ResolvedSecMetric:
    """Resolve a SEC series using legacy coverage/order/latest-filing rules."""

    known_order = tuple(candidate_tags or SEC_CANDIDATE_TAGS[metric])
    visible = [
        row for row in observations
        if row.get("metric") == metric
        and row.get("source") == "SEC"
        and row.get("source_variant") == "sec.company_facts"
        and row.get("normalizer_version") == "sec-normalized-v2"
        and row.get("selection_eligibility") == "ELIGIBLE"
        and row.get("observed_at") is not None
        and row["observed_at"] <= as_of
        and row.get("source_period_end") is not None
        and row["source_period_end"] >= window_start
        and row.get("source_metric_name")
    ]
    observed_tags = {str(row["source_metric_name"]) for row in visible}
    unknown_tags = (
        tuple(sorted(observed_tags.difference(known_order)))
        if candidate_tags is None and metric == "DILUTED_SHARES"
        else ()
    )
    ordered_tags = known_order + unknown_tags
    by_tag = {
        tag: [row for row in visible if row["source_metric_name"] == tag]
        for tag in ordered_tags
    }
    selected_tag = None
    best_coverage = 0
    for tag in ordered_tags:
        coverage = len({row["source_period_end"] for row in by_tag[tag]})
        if coverage > best_coverage:
            selected_tag = tag
            best_coverage = coverage
    selected = by_tag.get(selected_tag, [])
    latest = {}
    ambiguous_periods = []
    for row in selected:
        period = row["source_period_end"]
        filed = row.get("filed_date") or date.min
        if period not in latest or filed > latest[period][0]:
            latest[period] = (filed, [row])
        elif filed == latest[period][0]:
            latest[period][1].append(row)
    values = {}
    raw_ids = {}
    for period, (_, candidates) in sorted(latest.items()):
        candidate_values = {Decimal(str(item["value"])) for item in candidates}
        if len(candidate_values) > 1:
            ambiguous_periods.append(period)
            continue
        values[period] = next(iter(candidate_values))
        if len(candidates) == 1:
            raw_ids[period] = int(candidates[0]["raw_id"])
    return ResolvedSecMetric(
        metric=metric,
        selected_tag=selected_tag,
        values=values,
        raw_ids=raw_ids,
        shares_quality=_shares_quality(selected_tag) if metric == "DILUTED_SHARES" else "NOT_APPLICABLE",
        candidate_order_demonstrated=not bool(unknown_tags),
        ambiguous_periods=tuple(ambiguous_periods),
    )


def current_yoy_crosses_split(
    split_dates: Iterable[date],
    previous_period: date | None,
    latest_period: date | None,
    acquisition_status: str,
    completeness_status: str,
) -> bool | None:
    """Match legacy `_split_between`: (previous_period, latest_period]."""

    if acquisition_status != "SUCCESS" or completeness_status != "COMPLETE":
        return None
    if previous_period is None or latest_period is None:
        return None
    return any(previous_period < split_date <= latest_period for split_date in split_dates)


def _nearest(values: Mapping[date, Decimal], target: date | None, max_days: int) -> date | None:
    if target is None:
        return None
    candidates = [(abs((item - target).days), item) for item in values if abs((item - target).days) <= max_days]
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _implied_checks(
    eps: Mapping[date, Decimal],
    net_income: Mapping[date, Decimal],
    diluted_shares: Mapping[date, Decimal],
) -> list[dict]:
    checks = []
    for period, reported in sorted(eps.items()):
        ni_date = _nearest(net_income, period, 35)
        shares_date = _nearest(diluted_shares, period, 35)
        if ni_date is None or shares_date is None or diluted_shares[shares_date] == 0:
            continue
        implied = float(net_income[ni_date] / diluted_shares[shares_date])
        denominator = max(abs(float(reported)), 1e-9)
        difference = abs(float(reported) - implied) / denominator * 100.0
        checks.append({"date": period, "diff_pct": difference, "pass": difference <= SPLIT_INTEGRITY_TOLERANCE_PCT})
    return checks


def _check_near(checks: Sequence[dict], target: date | None, max_days: int = 45) -> dict | None:
    if target is None:
        return None
    candidates = [(abs((item["date"] - target).days), item) for item in checks]
    candidates = [item for item in candidates if item[0] <= max_days]
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def assess_split_integrity(
    acquisition_status: str,
    completeness_status: str,
    split_events: Sequence[tuple[date, Decimal]],
    eps: Mapping[date, Decimal],
    net_income: Mapping[date, Decimal],
    diluted_shares: Mapping[date, Decimal],
    shares_quality: str,
    *,
    candidate_order_demonstrated: bool,
) -> SplitIntegrityResult:
    """Apply the legacy formula without persisting its derived result."""

    if acquisition_status != "SUCCESS" or completeness_status != "COMPLETE":
        return SplitIntegrityResult("UNKNOWN", ())
    if not split_events:
        return SplitIntegrityResult("NO_RECENT_SPLITS", ())
    checks = _implied_checks(eps, net_income, diluted_shares)
    can_certify = (
        candidate_order_demonstrated
        and shares_quality in {"DILUTED_EXACT", "BASIC_AND_DILUTED"}
    )
    details = []
    for split_date, split_ratio in sorted(split_events):
        before_dates = [item for item in eps if item < split_date]
        after_dates = [item for item in eps if item >= split_date]
        before = max(before_dates) if before_dates else None
        after = min(after_dates) if after_dates else None
        before_check = _check_near(checks, before)
        after_check = _check_near(checks, after)
        shares_before_date = _nearest(diluted_shares, before, 35)
        shares_after_date = _nearest(diluted_shares, after, 35)
        shares_before = float(diluted_shares[shares_before_date]) if shares_before_date else None
        shares_after = float(diluted_shares[shares_after_date]) if shares_after_date else None
        observed_ratio = shares_after / shares_before if shares_before and shares_after else None
        both_pass = bool(before_check and after_check and before_check["pass"] and after_check["pass"])
        if observed_ratio is None:
            distance_to_one = distance_to_split = None
        else:
            distance_to_one = abs(math.log(max(observed_ratio, 1e-12)))
            distance_to_split = abs(math.log(max(observed_ratio, 1e-12) / max(float(split_ratio), 1e-12)))
        if not can_certify:
            status = "REVIEW_REQUIRED"
        elif both_pass and observed_ratio is not None and distance_to_one < distance_to_split:
            status = "ALREADY_ADJUSTED"
        elif both_pass and observed_ratio is not None and distance_to_split < distance_to_one:
            status = "UNADJUSTED"
        else:
            status = "REVIEW_REQUIRED"
        details.append({
            "date": split_date.isoformat(),
            "ratio": float(split_ratio),
            "status": status,
            "shares_quality": shares_quality,
            "before_eps_date": before.isoformat() if before else None,
            "after_eps_date": after.isoformat() if after else None,
            "before_eps_diff_pct": before_check["diff_pct"] if before_check else None,
            "after_eps_diff_pct": after_check["diff_pct"] if after_check else None,
            "diluted_shares_observed_ratio": observed_ratio,
        })
    statuses = {item["status"] for item in details}
    if statuses == {"ALREADY_ADJUSTED"}:
        aggregate = "VERIFIED_ALREADY_ADJUSTED"
    elif "UNADJUSTED" in statuses:
        aggregate = "UNADJUSTED_DETECTED"
    else:
        aggregate = "REVIEW_REQUIRED"
    return SplitIntegrityResult(aggregate, tuple(details))


def _capture_value(capture: object, name: str, default=None):
    if isinstance(capture, Mapping):
        return capture.get(name, default)
    return getattr(capture, name, default)


def _event_value(event: object, name: str, default=None):
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def select_latest_persisted_capture_as_of(
    captures: Iterable[object], as_of: datetime,
) -> object | None:
    """Return the latest visible attempt without status-based fallback."""
    eligible = [
        capture for capture in captures
        if (_capture_value(capture, "observed_at") is not None
            and _capture_value(capture, "observed_at") <= as_of)
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda capture: (
            _capture_value(capture, "observed_at"),
            _capture_value(capture, "id", 0) or 0,
        ),
    )


def reconstruct_split_integrity(
    captures: Iterable[object],
    events_by_capture_id: Mapping[object, Sequence[object]],
    as_of: datetime,
    eps: Mapping[date, Decimal],
    net_income: Mapping[date, Decimal],
    diluted_shares: Mapping[date, Decimal],
    shares_quality: str,
    *,
    candidate_order_demonstrated: bool,
) -> SplitIntegrityResult:
    """Select the latest persisted attempt and replay the split contract.

    The latest visible attempt wins regardless of outcome.  An unsuccessful
    attempt therefore returns UNKNOWN instead of falling back to an older
    successful capture.
    """
    selected = select_latest_persisted_capture_as_of(captures, as_of)
    if selected is None:
        return SplitIntegrityResult("UNKNOWN", ())
    capture_id = _capture_value(selected, "id")
    acquisition_status = _capture_value(selected, "acquisition_status")
    completeness_status = _capture_value(selected, "completeness_status")
    raw_events = events_by_capture_id.get(capture_id, ())
    split_events = []
    for event in raw_events:
        event_date = _event_value(event, "event_date")
        ratio = _event_value(event, "split_ratio")
        if event_date is not None and ratio is not None:
            split_events.append((event_date, Decimal(str(ratio))))
    return assess_split_integrity(
        acquisition_status,
        completeness_status,
        tuple(sorted(split_events)),
        eps,
        net_income,
        diluted_shares,
        shares_quality,
        candidate_order_demonstrated=candidate_order_demonstrated,
    )
