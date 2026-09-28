import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from database.corporate_actions import (
    FINGERPRINT_VERSION,
    CaptureEvidence,
    CorporateActionEvent,
    IdempotencyConflict,
    build_response_fingerprint,
    decide_idempotent_write,
    select_latest_capture_as_of,
    sanitize_error_metadata,
    validate_capture_bundle,
)


UTC = timezone.utc


def capture(**overrides):
    item = CaptureEvidence(
        capture_uid=UUID("11111111-1111-4111-8111-111111111111"),
        company_id=1,
        provider="YAHOO_FINANCE",
        source_variant="yfinance.splits",
        capture_contract_version="legacy-split-capture-v1",
        requested_window_start_at=datetime(2019, 9, 23, 10, 0, tzinfo=UTC),
        requested_window_end_at=None,
        capture_started_at=datetime(2025, 9, 23, 10, 0, tzinfo=UTC),
        capture_completed_at=datetime(2025, 9, 23, 10, 0, 2, tzinfo=UTC),
        observed_at=datetime(2025, 9, 23, 10, 0, 2, tzinfo=UTC),
        source_available_at=None,
        acquisition_status="SUCCESS",
        completeness_status="COMPLETE",
        provider_capture_id=None,
        provider_revision_id=None,
        provider_metadata={"symbol": "AAPL", "numeric": Decimal("2.00")},
        error_code=None,
        error_metadata={},
        response_fingerprint_version=FINGERPRINT_VERSION,
        response_fingerprint=None,
    )
    item = replace(item, **overrides)
    events = overrides.pop("events", ()) if "events" in overrides else ()
    return item, events


def event(ordinal=0, event_date=date(2020, 8, 31), ratio="4"):
    return CorporateActionEvent(
        event_ordinal=ordinal,
        action_type="STOCK_SPLIT",
        event_date=event_date,
        split_ratio=Decimal(ratio),
        provider_event_id=None,
        provider_revision_id=None,
        provider_metadata={},
    )


def finalized(item, events):
    return replace(item, response_fingerprint=build_response_fingerprint(item, events))


class FingerprintContractTests(unittest.TestCase):
    def test_v1_is_stable_across_metadata_order_and_decimal_spelling(self):
        first, _ = capture(provider_metadata={"b": 2, "a": Decimal("2.00")})
        second, _ = capture(provider_metadata={"a": Decimal("2.0"), "b": 2})
        events = (event(),)
        self.assertEqual(build_response_fingerprint(first, events), build_response_fingerprint(second, events))
        self.assertRegex(build_response_fingerprint(first, events), r"^[0-9a-f]{64}$")

    def test_numeric_metadata_and_literal_number_object_have_distinct_namespaces(self):
        first, _ = capture(provider_metadata={"value": Decimal("2")})
        second, _ = capture(provider_metadata={"value": {"$number": "2"}})
        self.assertNotEqual(build_response_fingerprint(first, (event(),)), build_response_fingerprint(second, (event(),)))

    def test_event_order_is_canonical_by_ordinal(self):
        item, _ = capture()
        first = event(0, date(2020, 8, 31), "4.0")
        second = event(1, date(2024, 6, 10), "2")
        self.assertEqual(
            build_response_fingerprint(item, (first, second)),
            build_response_fingerprint(item, (second, first)),
        )

    def test_timing_and_database_identity_are_not_response_material(self):
        item, _ = capture()
        shifted = replace(
            item,
            capture_started_at=datetime(2025, 9, 23, 11, tzinfo=UTC),
            capture_completed_at=datetime(2025, 9, 23, 11, 0, 2, tzinfo=UTC),
            observed_at=datetime(2025, 9, 23, 11, 0, 2, tzinfo=UTC),
            id=99,
        )
        self.assertEqual(build_response_fingerprint(item, (event(),)), build_response_fingerprint(shifted, (event(),)))

    def test_provider_revision_and_event_evidence_change_the_fingerprint(self):
        item, _ = capture()
        baseline = build_response_fingerprint(item, (event(),))
        revised = replace(item, provider_revision_id="revision-2")
        self.assertNotEqual(baseline, build_response_fingerprint(revised, (event(),)))
        self.assertNotEqual(baseline, build_response_fingerprint(item, (event(ratio="3"),)))


class CaptureContractTests(unittest.TestCase):
    def test_successful_empty_capture_is_valid_and_distinct_from_failure(self):
        item, _ = capture()
        item = finalized(item, ())
        validate_capture_bundle(item, ())
        failed = replace(
            item,
            acquisition_status="FAILED",
            completeness_status="UNKNOWN",
            error_code="PROVIDER_UNAVAILABLE",
            response_fingerprint_version=None,
            response_fingerprint=None,
        )
        validate_capture_bundle(failed, ())
        self.assertNotEqual(item.acquisition_status, failed.acquisition_status)

    def test_failed_or_unknown_capture_cannot_invent_events(self):
        item, _ = capture(
            acquisition_status="FAILED",
            completeness_status="UNKNOWN",
            error_code="PROVIDER_UNAVAILABLE",
            response_fingerprint_version=None,
        )
        with self.assertRaisesRegex(ValueError, "events"):
            validate_capture_bundle(item, (event(),))

    def test_ordinals_must_be_contiguous_and_window_lower_bound_is_enforced(self):
        item, _ = capture()
        item = finalized(item, (event(1),))
        with self.assertRaisesRegex(ValueError, "ordinal"):
            validate_capture_bundle(item, (event(1),))
        old = event(event_date=date(2018, 1, 1))
        item = finalized(item, (old,))
        with self.assertRaisesRegex(ValueError, "window"):
            validate_capture_bundle(item, (old,))

    def test_persisted_fingerprint_must_match_reconstructed_evidence(self):
        item, _ = capture()
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            validate_capture_bundle(replace(item, response_fingerprint="0" * 64), (event(),))

    def test_error_metadata_allowlist_drops_messages_and_secrets(self):
        self.assertEqual(
            sanitize_error_metadata({
                "exception_type": "TimeoutError",
                "retryable": True,
                "message": "token=secret",
                "password": "secret",
            }),
            {"exception_type": "TimeoutError", "retryable": True},
        )

    def test_unsanitized_error_metadata_is_rejected_before_persistence(self):
        item, _ = capture(
            acquisition_status="FAILED",
            completeness_status="UNKNOWN",
            error_code="TIMEOUT",
            error_metadata={"exception_type": "TimeoutError", "message": "token=secret"},
            response_fingerprint_version=None,
        )
        with self.assertRaisesRegex(ValueError, "sanitized"):
            validate_capture_bundle(item, ())

    def test_error_code_must_be_a_bounded_symbol_not_a_message(self):
        item, _ = capture(
            acquisition_status="FAILED",
            completeness_status="UNKNOWN",
            error_code="timeout token=secret",
            response_fingerprint_version=None,
        )
        with self.assertRaisesRegex(ValueError, "error_code"):
            validate_capture_bundle(item, ())

    def test_legacy_contract_requires_six_year_lower_and_open_upper_window(self):
        item, _ = capture(requested_window_end_at=datetime(2025, 9, 23, tzinfo=UTC))
        item = finalized(item, ())
        with self.assertRaisesRegex(ValueError, "open upper"):
            validate_capture_bundle(item, ())
        item, _ = capture(requested_window_start_at=datetime(2019, 9, 22, tzinfo=UTC))
        item = finalized(item, ())
        with self.assertRaisesRegex(ValueError, "six-year"):
            validate_capture_bundle(item, ())


class IdempotencyTests(unittest.TestCase):
    def test_identical_retry_returns_existing_without_write(self):
        item, _ = capture()
        item = finalized(item, (event(),))
        self.assertEqual(decide_idempotent_write(item, (event(),), item, (event(),)), "RETURN_EXISTING")

    def test_same_uid_with_any_different_immutable_evidence_conflicts(self):
        item, _ = capture()
        item = finalized(item, (event(),))
        changed = replace(item, provider_revision_id="revision-2")
        changed = finalized(changed, (event(),))
        with self.assertRaises(IdempotencyConflict):
            decide_idempotent_write(changed, (event(),), item, (event(),))

    def test_new_uid_inserts(self):
        item, _ = capture()
        item = finalized(item, ())
        self.assertEqual(decide_idempotent_write(item, (), None, ()), "INSERT")


class PointInTimeTests(unittest.TestCase):
    def test_latest_attempt_wins_even_when_it_failed(self):
        success, _ = capture()
        success = replace(finalized(success, ()), id=1)
        failed = replace(
            success,
            capture_uid=UUID("22222222-2222-4222-8222-222222222222"),
            acquisition_status="FAILED",
            completeness_status="UNKNOWN",
            error_code="TIMEOUT",
            response_fingerprint_version=None,
            response_fingerprint=None,
            capture_completed_at=datetime(2025, 9, 24, tzinfo=UTC),
            observed_at=datetime(2025, 9, 24, tzinfo=UTC),
            id=2,
        )
        self.assertIs(select_latest_capture_as_of((success, failed), datetime(2025, 9, 25, tzinfo=UTC)), failed)
        self.assertIs(select_latest_capture_as_of((success, failed), datetime(2025, 9, 23, 12, tzinfo=UTC)), success)


if __name__ == "__main__":
    unittest.main()
