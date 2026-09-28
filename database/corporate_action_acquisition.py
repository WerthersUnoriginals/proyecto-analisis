"""Single-attempt yfinance stock-split acquisition orchestration.

Provider access is lazy and injected in tests. Persistence remains exclusively
owned by :mod:`database.corporate_actions`.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping
from uuid import UUID, uuid5

from database.corporate_actions import (
    CAPTURE_CONTRACT_VERSION,
    FINGERPRINT_VERSION,
    PROVIDER,
    SOURCE_VARIANT,
    CaptureEvidence,
    CorporateActionEvent,
    IdempotencyConflict,
    build_response_fingerprint,
    load_capture_by_uid,
    record_capture_bundle,
)

_CAPTURE_NAMESPACE = UUID("4c3b6a78-6a0f-5f6d-9f7d-2f1c7e8f7b0a")


class ProviderAcquisitionError(RuntimeError):
    """An expected failure at the external provider adapter boundary."""


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def acquisition_window_start(as_of: datetime) -> tuple[datetime, None]:
    """Return the exact six-year lower bound and an open upper bound."""
    as_of = _ensure_utc(as_of)
    try:
        start = as_of.replace(year=as_of.year - 6)
    except ValueError:
        start = as_of.replace(year=as_of.year - 6, day=28)
    return start, None


def deterministic_capture_uid(company_id: int, ticker: str, as_of: datetime) -> UUID:
    start, _ = acquisition_window_start(as_of)
    material = f"{company_id}|{ticker.strip().upper()}|{PROVIDER}|{SOURCE_VARIANT}|{start.isoformat()}|{_ensure_utc(as_of).isoformat()}"
    return uuid5(_CAPTURE_NAMESPACE, material)


def _provider_items(response):
    if response is None:
        raise ValueError("provider returned no usable split series")
    if isinstance(response, Mapping):
        return list(response.items())
    items = getattr(response, "items", None)
    if callable(items):
        return list(items())
    if isinstance(response, (list, tuple)):
        return list(response)
    raise ValueError("provider returned an unsupported split series")


def _event_date(value) -> date:
    if isinstance(value, datetime):
        return _ensure_utc(value).date()
    if isinstance(value, date):
        return value
    candidate = getattr(value, "to_pydatetime", None)
    if callable(candidate):
        return _ensure_utc(candidate()).date()
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError("provider returned an invalid split date") from exc


def _ratio(value) -> Decimal:
    if isinstance(value, str) and ":" in value:
        numerator, denominator = value.split(":", 1)
        value = Decimal(numerator) / Decimal(denominator)
    try:
        ratio = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, ZeroDivisionError) as exc:
        raise ValueError("provider returned an invalid split ratio") from exc
    if not ratio.is_finite() or ratio <= 0:
        raise ValueError("split ratio must be finite and positive")
    return ratio


def normalize_split_events(
    response, as_of: datetime, *, window_start: datetime | None = None,
) -> tuple[CorporateActionEvent, ...]:
    start = window_start or acquisition_window_start(as_of)[0]
    upper = _ensure_utc(as_of).date()
    unique = {}
    for raw_date, raw_ratio in _provider_items(response):
        if isinstance(raw_date, (tuple, list)) and len(raw_date) == 2 and raw_ratio is None:
            raw_date, raw_ratio = raw_date
        event_date = _event_date(raw_date)
        ratio = _ratio(raw_ratio)
        if event_date < start.date() or event_date > upper:
            continue
        key = (event_date, str(ratio.normalize()))
        if key in unique:
            continue
        if any(item[0] == event_date for item in unique):
            raise ValueError("conflicting split ratios for one event date")
        unique[key] = (event_date, ratio)
    ordered = sorted(unique.values(), key=lambda item: (item[0], item[1]))
    return tuple(
        CorporateActionEvent(
            event_ordinal=index,
            action_type="STOCK_SPLIT",
            event_date=event_date,
            split_ratio=ratio,
            provider_event_id=f"{event_date.isoformat()}:{ratio}",
            provider_revision_id=None,
            provider_metadata={"source": SOURCE_VARIANT},
        )
        for index, (event_date, ratio) in enumerate(ordered)
    )


def _fetch_yfinance_splits(ticker: str):
    expected_provider_errors = (ImportError, OSError)
    try:
        import yfinance as yf
        try:
            from yfinance.exceptions import YFRateLimitError
        except (ImportError, AttributeError):
            YFRateLimitError = None
        if (
            isinstance(YFRateLimitError, type)
            and issubclass(YFRateLimitError, BaseException)
        ):
            expected_provider_errors += (YFRateLimitError,)
        return yf.Ticker(ticker).splits
    except expected_provider_errors as exc:
        # Only dependency, transport, and provider rate-limit failures are
        # provider-boundary errors; programming failures must propagate.
        raise ProviderAcquisitionError("yfinance split acquisition failed") from exc


def _failure_capture(uid, company_id, ticker, as_of, observed_at, error_code):
    start, end = acquisition_window_start(observed_at)
    return CaptureEvidence(
        capture_uid=uid, company_id=company_id, provider=PROVIDER,
        source_variant=SOURCE_VARIANT, capture_contract_version=CAPTURE_CONTRACT_VERSION,
        requested_window_start_at=start, requested_window_end_at=end,
        capture_started_at=observed_at, capture_completed_at=observed_at,
        observed_at=observed_at, source_available_at=None,
        acquisition_status="FAILED", completeness_status="UNKNOWN",
        provider_capture_id=f"{ticker.strip().upper()}:{_ensure_utc(as_of).isoformat()}",
        provider_revision_id=None, provider_metadata={"ticker": ticker.upper()},
        error_code=error_code, error_metadata={"operation": "yfinance.splits"},
        response_fingerprint_version=None, response_fingerprint=None,
    )


def acquire_and_record_stock_splits(
    company_id: int,
    ticker: str,
    as_of: datetime,
    *,
    fetch_splits: Callable[[str], object] | None = None,
    lookup_capture: Callable[[UUID], tuple[CaptureEvidence | None, tuple[CorporateActionEvent, ...]]] | None = None,
    clock: Callable[[], datetime] | None = None,
    repository: Callable | None = None,
) -> dict:
    """Acquire exactly one ticker's splits and persist one immutable attempt."""
    as_of = _ensure_utc(as_of)
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    clock = clock or (lambda: datetime.now(timezone.utc))
    repository = repository or record_capture_bundle
    uid = deterministic_capture_uid(company_id, ticker, as_of)
    lookup = lookup_capture or load_capture_by_uid
    existing, existing_events = lookup(uid)
    if existing is not None:
        return _result(existing, existing_events, as_of, "RECOVERED_IDEMPOTENT")
    observed_at = _ensure_utc(clock())
    try:
        response = (fetch_splits or _fetch_yfinance_splits)(ticker)
    except ProviderAcquisitionError:
        capture = _failure_capture(uid, company_id, ticker, as_of, observed_at, "PROVIDER_FAILURE")
        events = ()
    else:
        start, end = acquisition_window_start(observed_at)
        try:
            events = normalize_split_events(response, as_of, window_start=start)
        except ValueError:
            capture = _failure_capture(uid, company_id, ticker, as_of, observed_at, "PROVIDER_FAILURE")
            events = ()
        else:
            capture = CaptureEvidence(
                capture_uid=uid, company_id=company_id, provider=PROVIDER,
                source_variant=SOURCE_VARIANT, capture_contract_version=CAPTURE_CONTRACT_VERSION,
                requested_window_start_at=start, requested_window_end_at=end,
                capture_started_at=observed_at, capture_completed_at=observed_at,
                observed_at=observed_at, source_available_at=None,
                acquisition_status="SUCCESS", completeness_status="COMPLETE",
                provider_capture_id=f"{ticker}:{as_of.isoformat()}", provider_revision_id=None,
                provider_metadata={"ticker": ticker, "window_upper_bound": None},
                error_code=None, error_metadata={}, response_fingerprint_version=FINGERPRINT_VERSION,
                response_fingerprint=None,
            )
            capture = replace(capture, response_fingerprint=build_response_fingerprint(capture, events))
    try:
        stored_capture, stored_events = repository(capture, events)
    except IdempotencyConflict:
        return {"company_id": company_id, "ticker": ticker, "provider": PROVIDER,
                "source_variant": SOURCE_VARIANT, "as_of": as_of,
                "capture_uid": uid, "capture_id": None, "acquisition_status": capture.acquisition_status,
                "completeness_status": capture.completeness_status, "event_count": 0,
                "events": (), "repository_outcome": "IDEMPOTENCY_CONFLICT"}
    return _result(stored_capture, stored_events, as_of, "PERSISTED")


def _result(capture, events, as_of, repository_outcome):
    return {
        "company_id": capture.company_id, "ticker": capture.provider_metadata.get("ticker"),
        "provider": capture.provider, "source_variant": capture.source_variant,
        "as_of": as_of, "observed_at": capture.observed_at,
        "capture_uid": capture.capture_uid, "capture_id": capture.id,
        "acquisition_status": capture.acquisition_status,
        "completeness_status": capture.completeness_status, "event_count": len(events),
        "events": tuple({"event_ordinal": event.event_ordinal, "event_date": event.event_date,
                          "split_ratio": event.split_ratio} for event in events),
        "repository_outcome": repository_outcome,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Acquire one ticker's corporate-action splits")
    parser.add_argument("ticker", nargs="?", default="AAPL")
    args = parser.parse_args(argv)
    from database.db import get_connection
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE ticker = %s", (args.ticker.upper(),))
            company = cursor.fetchone()
    if company is None:
        raise SystemExit(f"Ticker not found: {args.ticker.upper()}")
    result = acquire_and_record_stock_splits(company[0], args.ticker, datetime.now(timezone.utc))
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
