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
    load_capture_by_uid,
    load_latest_capture_as_of,
    record_capture_bundle,
)


UTC = timezone.utc


def bundle(uid="11111111-1111-4111-8111-111111111111"):
    event = CorporateActionEvent(0, "STOCK_SPLIT", date(2020, 8, 31), Decimal("4"), None, None, {})
    capture = CaptureEvidence(
        UUID(uid), 1, "YAHOO_FINANCE", "yfinance.splits", "legacy-split-capture-v1",
        datetime(2019, 9, 23, 10, tzinfo=UTC), None,
        datetime(2025, 9, 23, 10, tzinfo=UTC),
        datetime(2025, 9, 23, 10, 0, 2, tzinfo=UTC),
        datetime(2025, 9, 23, 10, 0, 2, tzinfo=UTC),
        None, "SUCCESS", "COMPLETE", None, None, {"symbol": "AAPL"}, None, {},
        FINGERPRINT_VERSION, None,
    )
    return replace(capture, response_fingerprint=build_response_fingerprint(capture, (event,))), (event,)


def capture_row(item, row_id=41):
    return {**item.__dict__, "id": row_id}


def event_row(item, row_id=91, capture_id=41):
    return {**item.__dict__, "id": row_id, "capture_id": capture_id}


class FakeCursor:
    def __init__(self, fetchones=(), fetchalls=()):
        self.fetchones = iter(fetchones)
        self.fetchalls = iter(fetchalls)
        self.executed = []
        self.executemany_calls = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def executemany(self, sql, params):
        self.executemany_calls.append((sql, list(params)))

    def fetchone(self):
        return next(self.fetchones)

    def fetchall(self):
        return next(self.fetchalls)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.entered = 0
        self.exited = 0
        self.cursor_kwargs = []

    def cursor(self, **kwargs):
        self.cursor_kwargs.append(kwargs)
        return self._cursor

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_):
        self.exited += 1
        return False


class RepositoryTests(unittest.TestCase):
    def test_inserts_parent_and_children_in_one_connection_context(self):
        item, events = bundle()
        cursor = FakeCursor(fetchones=(None, {"id": 41}))
        connection = FakeConnection(cursor)
        stored, stored_events = record_capture_bundle(item, events, connection_factory=lambda: connection)
        self.assertEqual(stored.id, 41)
        self.assertEqual(stored_events[0].capture_id, 41)
        self.assertEqual(connection.entered, 1)
        self.assertEqual(connection.exited, 1)
        self.assertIn("row_factory", connection.cursor_kwargs[0])
        self.assertEqual(sum("INSERT INTO public.corporate_action_captures" in sql for sql, _ in cursor.executed), 1)
        self.assertEqual(len(cursor.executemany_calls), 1)
        insert_params = next(params for sql, params in cursor.executed if "INSERT INTO public.corporate_action_captures" in sql)
        metadata_param = insert_params[15]
        self.assertEqual(metadata_param.obj, {"symbol": "AAPL"})

    def test_identical_retry_returns_existing_without_insert(self):
        item, events = bundle()
        cursor = FakeCursor(fetchones=(capture_row(item),), fetchalls=([event_row(events[0])],))
        stored, _ = record_capture_bundle(item, events, connection_factory=lambda: FakeConnection(cursor))
        self.assertEqual(stored.id, 41)
        self.assertTrue(all("INSERT" not in sql for sql, _ in cursor.executed))
        self.assertFalse(cursor.executemany_calls)

    def test_conflicting_retry_raises_before_any_insert(self):
        item, events = bundle()
        existing = replace(item, provider_revision_id="revision-2")
        existing = replace(existing, response_fingerprint=build_response_fingerprint(existing, events))
        cursor = FakeCursor(fetchones=(capture_row(existing),), fetchalls=([event_row(events[0])],))
        with self.assertRaises(IdempotencyConflict):
            record_capture_bundle(item, events, connection_factory=lambda: FakeConnection(cursor))
        self.assertTrue(all("INSERT" not in sql for sql, _ in cursor.executed))

    def test_loader_uses_latest_attempt_and_its_children_only(self):
        item, events = bundle()
        failed = replace(
            item,
            id=44,
            capture_uid=UUID("22222222-2222-4222-8222-222222222222"),
            acquisition_status="FAILED",
            completeness_status="UNKNOWN",
            error_code="TIMEOUT",
            response_fingerprint_version=None,
            response_fingerprint=None,
        )
        cursor = FakeCursor(fetchones=(capture_row(failed, 44),), fetchalls=([],))
        loaded, loaded_events = load_latest_capture_as_of(
            1, datetime(2025, 9, 24, tzinfo=UTC), connection_factory=lambda: FakeConnection(cursor)
        )
        self.assertEqual(loaded.acquisition_status, "FAILED")
        self.assertEqual(loaded_events, ())
        self.assertEqual(len(cursor.executed), 2)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql, _ in cursor.executed))

    def test_public_uid_preflight_returns_bundle_without_any_write(self):
        item, events = bundle()
        cursor = FakeCursor(fetchones=(capture_row(item),), fetchalls=([event_row(events[0])],))
        loaded, loaded_events = load_capture_by_uid(
            item.capture_uid, connection_factory=lambda: FakeConnection(cursor)
        )
        self.assertEqual(loaded.id, 41)
        self.assertEqual(len(loaded_events), 1)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql, _ in cursor.executed))

    def test_public_uid_preflight_rejects_corrupt_persisted_fingerprint(self):
        item, events = bundle()
        corrupt = replace(item, response_fingerprint="0" * 64)
        cursor = FakeCursor(fetchones=(capture_row(corrupt),), fetchalls=([event_row(events[0])],))
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            load_capture_by_uid(item.capture_uid, connection_factory=lambda: FakeConnection(cursor))

    def test_decimal_metadata_is_stored_in_canonical_jsonb_form(self):
        item, events = bundle()
        item = replace(item, provider_metadata={"ratio": Decimal("2.00")})
        item = replace(item, response_fingerprint=build_response_fingerprint(item, events))
        cursor = FakeCursor(fetchones=(None, {"id": 41}))
        record_capture_bundle(item, events, connection_factory=lambda: FakeConnection(cursor))
        insert_params = next(params for sql, params in cursor.executed if "INSERT INTO public.corporate_action_captures" in sql)
        self.assertEqual(insert_params[15].obj, {"ratio": {"$type": "number", "value": "2"}})

    def test_all_repository_sql_qualifies_public_relations(self):
        from database.corporate_actions import (
            INSERT_CAPTURE_SQL,
            INSERT_EVENT_SQL,
            LOAD_CAPTURE_BY_UID_SQL,
            LOAD_EVENTS_SQL,
            LOAD_LATEST_CAPTURE_SQL,
        )
        statements = (
            LOAD_CAPTURE_BY_UID_SQL,
            LOAD_LATEST_CAPTURE_SQL,
            LOAD_EVENTS_SQL,
            INSERT_CAPTURE_SQL,
            INSERT_EVENT_SQL,
        )
        for sql in statements:
            self.assertNotRegex(sql, r"\b(?:FROM|INTO)\s+(?!public\.)corporate_action_")
        self.assertIn("FROM public.corporate_action_captures", LOAD_CAPTURE_BY_UID_SQL)
        self.assertIn("FROM public.corporate_action_captures", LOAD_LATEST_CAPTURE_SQL)
        self.assertIn("FROM public.corporate_action_events", LOAD_EVENTS_SQL)
        self.assertIn("INTO public.corporate_action_captures", INSERT_CAPTURE_SQL)
        self.assertIn("INTO public.corporate_action_events", INSERT_EVENT_SQL)


if __name__ == "__main__":
    unittest.main()
