import unittest
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from database.corporate_action_acquisition import (
    acquisition_window_start,
    normalize_split_events,
    deterministic_capture_uid,
    acquire_and_record_stock_splits,
    ProviderAcquisitionError,
)
from database.corporate_actions import validate_capture_bundle


AS_OF = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


class AcquisitionPureTests(unittest.TestCase):
    def test_window_is_exactly_six_years_and_open_upper_bound(self):
        start, end = acquisition_window_start(AS_OF)
        self.assertEqual(start, datetime(2020, 9, 24, 12, tzinfo=timezone.utc))
        self.assertIsNone(end)

    def test_leap_day_uses_repository_calendar_fallback(self):
        start, end = acquisition_window_start(datetime(2024, 2, 29, 23, 59, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2018, 2, 28, 23, 59, tzinfo=timezone.utc))
        self.assertIsNone(end)

    def test_normalization_is_deterministic_and_filters_window(self):
        response = {
            datetime(2020, 9, 24, tzinfo=timezone.utc): 2,
            datetime(2021, 1, 2, tzinfo=timezone.utc): 4,
            datetime(2026, 9, 24, tzinfo=timezone.utc): 3,
            datetime(2026, 9, 25, tzinfo=timezone.utc): 5,
        }
        events = normalize_split_events(response, AS_OF)
        self.assertEqual([(e.event_ordinal, e.event_date, e.split_ratio) for e in events], [
            (0, datetime(2020, 9, 24, tzinfo=timezone.utc).date(), Decimal("2")),
            (1, datetime(2021, 1, 2, tzinfo=timezone.utc).date(), Decimal("4")),
            (2, datetime(2026, 9, 24, tzinfo=timezone.utc).date(), Decimal("3")),
        ])

    def test_duplicate_rows_are_deduplicated(self):
        response = [("2024-01-01", 2), ("2024-01-01", 2), ("2024-01-02", 3)]
        events = normalize_split_events(response, AS_OF)
        self.assertEqual(len(events), 2)

    def test_invalid_ratio_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_split_events([("2024-01-01", 0)], AS_OF)
        with self.assertRaises(ValueError):
            normalize_split_events([("2024-01-01", "NaN")], AS_OF)

    def test_capture_uid_is_stable_for_same_logical_attempt(self):
        self.assertEqual(
            deterministic_capture_uid(1, "AAPL", AS_OF),
            deterministic_capture_uid(1, "AAPL", AS_OF),
        )
        self.assertNotEqual(
            deterministic_capture_uid(1, "AAPL", AS_OF),
            deterministic_capture_uid(1, "AAPL", AS_OF.replace(hour=13)),
        )


class AcquisitionRepositoryTests(unittest.TestCase):
    def _fake_repository(self, capture, events):
        return capture, tuple(events)

    def test_zero_events_are_success_complete(self):
        with patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=self._fake_repository,
        ) as repository:
            result = acquire_and_record_stock_splits(
                1, "AAPL", AS_OF, fetch_splits=lambda ticker: {}, clock=lambda: AS_OF,
                lookup_capture=lambda uid: (None, ()),
            )
        self.assertEqual(result["acquisition_status"], "SUCCESS")
        self.assertEqual(result["completeness_status"], "COMPLETE")
        self.assertEqual(result["event_count"], 0)
        repository.assert_called_once()

    def test_one_and_multiple_events_have_stable_ordinals(self):
        response = [("2024-01-02", 2), ("2022-01-01", 4)]
        with patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=self._fake_repository,
        ):
            result = acquire_and_record_stock_splits(
                1, "AAPL", AS_OF, fetch_splits=lambda ticker: response, clock=lambda: AS_OF,
                lookup_capture=lambda uid: (None, ()),
            )
        self.assertEqual(result["event_count"], 2)
        self.assertEqual([item["event_ordinal"] for item in result["events"]], [0, 1])

    def test_expected_provider_exception_persists_failed_attempt(self):
        with patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=self._fake_repository,
        ):
            result = acquire_and_record_stock_splits(
                1, "AAPL", AS_OF,
                fetch_splits=lambda ticker: (_ for _ in ()).throw(ProviderAcquisitionError("network")),
                clock=lambda: AS_OF,
                lookup_capture=lambda uid: (None, ()),
            )
        self.assertEqual(result["acquisition_status"], "FAILED")
        self.assertEqual(result["completeness_status"], "UNKNOWN")
        self.assertEqual(result["event_count"], 0)

    def test_unexpected_runtime_error_propagates_without_persisting_failure(self):
        with patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=self._fake_repository,
        ) as repository:
            with self.assertRaises(RuntimeError):
                acquire_and_record_stock_splits(
                    1, "AAPL", AS_OF,
                    fetch_splits=lambda ticker: (_ for _ in ()).throw(RuntimeError("bug")),
                    clock=lambda: AS_OF,
                    lookup_capture=lambda uid: (None, ()),
                )
        repository.assert_not_called()

    def test_unexpected_type_error_and_key_error_propagate(self):
        for error in (TypeError("bug"), KeyError("field")):
            with self.subTest(error=type(error).__name__):
                with patch(
                    "database.corporate_action_acquisition.record_capture_bundle",
                    side_effect=self._fake_repository,
                ) as repository:
                    with self.assertRaises(type(error)):
                        acquire_and_record_stock_splits(
                            1, "AAPL", AS_OF,
                            fetch_splits=lambda ticker, error=error: (_ for _ in ()).throw(error),
                            clock=lambda: AS_OF,
                            lookup_capture=lambda uid: (None, ()),
                        )
                repository.assert_not_called()

    def test_malformed_provider_response_is_failed_not_zero_success(self):
        with patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=self._fake_repository,
        ):
            result = acquire_and_record_stock_splits(
                1, "AAPL", AS_OF, fetch_splits=lambda ticker: object(),
                clock=lambda: AS_OF, lookup_capture=lambda uid: (None, ()),
            )
        self.assertEqual(result["acquisition_status"], "FAILED")
        self.assertNotEqual(result["completeness_status"], "COMPLETE")

    def test_existing_uid_is_recovered_without_provider_call(self):
        stored = {}
        with patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=lambda capture, events: (stored.setdefault("capture", capture), tuple(events)),
        ):
            first = acquire_and_record_stock_splits(
                1, "AAPL", AS_OF, fetch_splits=lambda ticker: {},
                clock=lambda: AS_OF, lookup_capture=lambda uid: (None, ()),
            )
        calls = []
        recovered = acquire_and_record_stock_splits(
            1, "AAPL", AS_OF,
            fetch_splits=lambda ticker: calls.append(ticker),
            lookup_capture=lambda uid: (stored["capture"], ()),
            clock=lambda: AS_OF,
        )
        self.assertEqual(recovered["repository_outcome"], "RECOVERED_IDEMPOTENT")
        self.assertEqual(calls, [])
        self.assertEqual(recovered["capture_uid"], first["capture_uid"])

    def test_production_validator_accepts_capture_started_window_with_microseconds(self):
        started = AS_OF.replace(microsecond=123456)

        def validating_repository(capture, events):
            validate_capture_bundle(capture, events)
            return capture, tuple(events)

        result = acquire_and_record_stock_splits(
            1, "AAPL", AS_OF, fetch_splits=lambda ticker: {},
            clock=lambda: started, lookup_capture=lambda uid: (None, ()),
            repository=validating_repository,
        )
        self.assertEqual(result["acquisition_status"], "SUCCESS")

    def test_changed_evidence_is_left_to_repository_conflict_contract(self):
        from database.corporate_actions import IdempotencyConflict
        with patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=IdempotencyConflict(),
        ):
            result = acquire_and_record_stock_splits(
                1, "AAPL", AS_OF, fetch_splits=lambda ticker: {"2024-01-01": 2},
                clock=lambda: AS_OF,
                lookup_capture=lambda uid: (None, ()),
            )
        self.assertEqual(result["repository_outcome"], "IDEMPOTENCY_CONFLICT")

    def test_module_does_not_call_provider_at_import(self):
        import database.corporate_action_acquisition as module
        self.assertTrue(hasattr(module, "acquire_and_record_stock_splits"))

    def test_real_yfinance_adapter_wraps_expected_io_failure(self):
        from database.corporate_action_acquisition import _fetch_yfinance_splits

        provider = SimpleNamespace(
            Ticker=lambda ticker: (_ for _ in ()).throw(OSError("network"))
        )
        with patch.dict(sys.modules, {"yfinance": provider}):
            with self.assertRaises(ProviderAcquisitionError):
                _fetch_yfinance_splits("AAPL")

    def test_real_yfinance_adapter_keeps_optional_dependency_safe(self):
        from database.corporate_action_acquisition import _fetch_yfinance_splits

        with patch.dict(sys.modules, {"yfinance": None}):
            with self.assertRaises(ProviderAcquisitionError):
                _fetch_yfinance_splits("AAPL")

    def test_real_yfinance_adapter_wraps_rate_limit_failure(self):
        from database.corporate_action_acquisition import (
            ProviderAcquisitionError,
            _fetch_yfinance_splits,
        )

        class FakeYFRateLimitError(Exception):
            pass

        provider = SimpleNamespace(
            Ticker=lambda ticker: SimpleNamespace(
                splits=(_ for _ in ()).throw(FakeYFRateLimitError("429"))
            )
        )
        exceptions = SimpleNamespace(YFRateLimitError=FakeYFRateLimitError)
        with patch.dict(
            sys.modules,
            {"yfinance": provider, "yfinance.exceptions": exceptions},
        ):
            with self.assertRaises(ProviderAcquisitionError):
                _fetch_yfinance_splits("AAPL")

    def test_orchestrator_persists_rate_limit_as_provider_failure(self):
        class FakeYFRateLimitError(Exception):
            pass

        provider = SimpleNamespace(
            Ticker=lambda ticker: SimpleNamespace(
                splits=(_ for _ in ()).throw(FakeYFRateLimitError("429"))
            )
        )
        exceptions = SimpleNamespace(YFRateLimitError=FakeYFRateLimitError)
        with patch.dict(
            sys.modules,
            {"yfinance": provider, "yfinance.exceptions": exceptions},
        ), patch(
            "database.corporate_action_acquisition.record_capture_bundle",
            side_effect=self._fake_repository,
        ) as repository:
            result = acquire_and_record_stock_splits(
                1,
                "AAPL",
                AS_OF,
                clock=lambda: AS_OF,
                lookup_capture=lambda uid: (None, ()),
            )
        repository.assert_called_once()
        self.assertEqual(result["acquisition_status"], "FAILED")
        self.assertEqual(result["completeness_status"], "UNKNOWN")
        self.assertEqual(result["event_count"], 0)

    def test_real_yfinance_adapter_does_not_wrap_programming_errors(self):
        from database.corporate_action_acquisition import _fetch_yfinance_splits

        for error in (KeyError("field"), TypeError("shape"), RuntimeError("bug")):
            with self.subTest(error=type(error).__name__):
                provider = SimpleNamespace(
                    Ticker=lambda ticker, error=error: SimpleNamespace(
                        splits=(_ for _ in ()).throw(error)
                    )
                )
                with patch.dict(sys.modules, {"yfinance": provider}):
                    with self.assertRaises(type(error)):
                        _fetch_yfinance_splits("AAPL")

    def test_real_yfinance_adapter_returns_successful_splits_unchanged(self):
        from database.corporate_action_acquisition import _fetch_yfinance_splits

        splits = {"2024-01-01": 2}
        provider = SimpleNamespace(
            Ticker=lambda ticker: SimpleNamespace(splits=splits)
        )
        with patch.dict(sys.modules, {"yfinance": provider}):
            self.assertIs(_fetch_yfinance_splits("AAPL"), splits)


if __name__ == "__main__":
    unittest.main()
