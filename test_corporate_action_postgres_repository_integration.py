"""Opt-in real PostgreSQL validation of the corporate-action repository API."""

from __future__ import annotations

import os
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from database.corporate_actions import (
    CAPTURE_CONTRACT_VERSION,
    FINGERPRINT_VERSION,
    PROVIDER,
    SOURCE_VARIANT,
    CaptureEvidence,
    CorporateActionEvent,
    IdempotencyConflict,
    build_response_fingerprint,
    record_capture_bundle,
)


RUN_FLAG = "CANSLIM_RUN_PG_INTEGRATION"
TEST_UIDS = (
    UUID("00000000-0000-0000-0000-00000000d301"),
    UUID("00000000-0000-0000-0000-00000000d302"),
    UUID("00000000-0000-0000-0000-00000000d303"),
    UUID("00000000-0000-0000-0000-00000000d304"),
    UUID("00000000-0000-0000-0000-00000000d305"),
)
LEGACY_CONCURRENCY_UIDS = (
    UUID("00000000-0000-0000-0000-00000000a001"),
    UUID("00000000-0000-0000-0000-00000000a002"),
)
RACE_TIMEOUT_SECONDS = 20.0


def _connect():
    from database.db import get_connection

    return get_connection()


def _capture(uid: UUID, metadata: dict | None = None) -> CaptureEvidence:
    started = datetime(2025, 3, 1, tzinfo=timezone.utc)
    completed = datetime(2025, 3, 1, 0, 0, 1, tzinfo=timezone.utc)
    capture = CaptureEvidence(
        capture_uid=uid,
        company_id=1,
        provider=PROVIDER,
        source_variant=SOURCE_VARIANT,
        capture_contract_version=CAPTURE_CONTRACT_VERSION,
        requested_window_start_at=datetime(2019, 3, 1, tzinfo=timezone.utc),
        requested_window_end_at=None,
        capture_started_at=started,
        capture_completed_at=completed,
        observed_at=completed,
        source_available_at=None,
        acquisition_status="SUCCESS",
        completeness_status="COMPLETE",
        provider_capture_id=None,
        provider_revision_id=None,
        provider_metadata=metadata or {},
        error_code=None,
        error_metadata={},
        response_fingerprint_version=FINGERPRINT_VERSION,
        response_fingerprint=None,
    )
    return replace(capture, response_fingerprint=build_response_fingerprint(capture, _events()))


def _events() -> tuple[CorporateActionEvent, ...]:
    return (
        CorporateActionEvent(
            event_ordinal=0,
            action_type="STOCK_SPLIT",
            event_date=date(2023, 6, 30),
            split_ratio=Decimal("2"),
            provider_event_id="10b3d-event-0",
            provider_revision_id=None,
            provider_metadata={},
        ),
        CorporateActionEvent(
            event_ordinal=1,
            action_type="STOCK_SPLIT",
            event_date=date(2024, 6, 30),
            split_ratio=Decimal("3"),
            provider_event_id="10b3d-event-1",
            provider_revision_id=None,
            provider_metadata={},
        ),
    )


def _capture_sql_rows(connection, uid: UUID) -> tuple[int, int, str | None, str | None]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id, count(e.id), c.provider, c.source_variant
            FROM public.corporate_action_captures AS c
            LEFT JOIN public.corporate_action_events AS e
              ON e.capture_id = c.id
            WHERE c.capture_uid = %s
            GROUP BY c.id, c.provider, c.source_variant
            """,
            (str(uid),),
        )
        row = cursor.fetchone()
    if row is None:
        return 0, 0, None, None
    return int(row[0]), int(row[1]), row[2], row[3]


def _cleanup_reserved() -> int:
    connection = _connect()
    try:
        values = [str(uid) for uid in TEST_UIDS]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM public.corporate_action_events AS e
                USING public.corporate_action_captures AS c
                WHERE c.id = e.capture_id
                  AND c.capture_uid = ANY(%s::uuid[])
                """,
                (values,),
            )
            cursor.execute(
                """
                DELETE FROM public.corporate_action_captures
                WHERE capture_uid = ANY(%s::uuid[])
                """,
                (values,),
            )
            cursor.execute(
                """
                SELECT count(*)
                FROM public.corporate_action_captures
                WHERE capture_uid = ANY(%s::uuid[])
                """,
                (values,),
            )
            remaining = int(cursor.fetchone()[0])
        connection.commit()
        if remaining:
            raise AssertionError(f"reserved rows remain after cleanup: {remaining}")
        return remaining
    finally:
        connection.close()


class _FailAfterParentCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def execute(self, *args, **kwargs):
        return self._cursor.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        raise RuntimeError("intentional child persistence failure")

    def fetchone(self):
        return self._cursor.fetchone()


class _FailAfterParentConnection:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def cursor(self, *args, **kwargs):
        return _FailAfterParentCursor(self._connection.cursor(*args, **kwargs))


class _BarrierCursor:
    def __init__(self, cursor, barrier):
        self._cursor = cursor
        self._barrier = barrier

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def execute(self, statement, params=None):
        if statement.lstrip().upper().startswith("INSERT INTO CORPORATE_ACTION_CAPTURES"):
            self._barrier.wait(timeout=RACE_TIMEOUT_SECONDS)
        return self._cursor.execute(statement, params)

    def executemany(self, *args, **kwargs):
        return self._cursor.executemany(*args, **kwargs)

    def fetchone(self):
        return self._cursor.fetchone()

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _BarrierConnection:
    def __init__(self, connection, barrier):
        self._connection = connection
        self._barrier = barrier

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def cursor(self, *args, **kwargs):
        return _BarrierCursor(self._connection.cursor(*args, **kwargs), self._barrier)


class CorporateActionRepositoryPostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv(RUN_FLAG) != "1":
            raise unittest.SkipTest(f"set {RUN_FLAG}=1 to run real PostgreSQL integration")

    @staticmethod
    def _preflight(connection):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user, version()")
            database_name, current_user, version = cursor.fetchone()
            if database_name != "canslim":
                raise AssertionError(f"unexpected database: {database_name}")
            if current_user != "canslim_app":
                raise AssertionError(f"unexpected user: {current_user}")
            if "PostgreSQL 17." not in version:
                raise AssertionError(f"unexpected PostgreSQL version: {version}")

            cursor.execute(
                """
                SELECT to_regclass('public.corporate_action_captures'),
                       to_regclass('public.corporate_action_events'),
                       to_regclass('public.corporate_action_captures_point_in_time_idx'),
                       to_regclass('public.corporate_action_captures_provider_revision_idx'),
                       to_regclass('public.corporate_action_events_capture_date_idx')
                """
            )
            if any(value is None for value in cursor.fetchone()):
                raise AssertionError("required corporate-action object is missing")

            cursor.execute(
                """
                SELECT c.relname, pg_get_userbyid(c.relowner)
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('corporate_action_captures', 'corporate_action_events')
                ORDER BY c.relname
                """
            )
            ownership = cursor.fetchall()
            if len(ownership) != 2 or any(owner != "canslim_app" for _, owner in ownership):
                raise AssertionError(f"unexpected ownership: {ownership}")

            for label, values in (
                ("legacy", LEGACY_CONCURRENCY_UIDS),
                ("task_10b3d", TEST_UIDS),
            ):
                uid_values = [str(uid) for uid in values]
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.corporate_action_captures
                    WHERE capture_uid = ANY(%s::uuid[])
                    """,
                    (uid_values,),
                )
                parent_count = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.corporate_action_events AS e
                    JOIN public.corporate_action_captures AS c
                      ON c.id = e.capture_id
                    WHERE c.capture_uid = ANY(%s::uuid[])
                    """,
                    (uid_values,),
                )
                event_count = int(cursor.fetchone()[0])
                print(f"PREFLIGHT_{label.upper()}_PARENTS={parent_count}")
                print(f"PREFLIGHT_{label.upper()}_EVENTS={event_count}")
                if parent_count or event_count:
                    raise AssertionError(f"reserved {label} rows are not zero")

            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM public.corporate_action_captures),
                    (SELECT count(*) FROM public.corporate_action_events)
                """
            )
            total_captures, total_events = cursor.fetchone()
            print(f"PREFLIGHT_TOTAL_CAPTURES={total_captures}")
            print(f"PREFLIGHT_TOTAL_EVENTS={total_events}")

    def test_real_repository_contract(self):
        preflight = _connect()
        try:
            self._preflight(preflight)
            self.assertEqual(_capture_sql_rows(preflight, TEST_UIDS[0])[0], 0)
        finally:
            preflight.close()

        _cleanup_reserved()
        try:
            uid = TEST_UIDS[0]
            capture = _capture(uid)
            events = _events()

            stored, stored_events = record_capture_bundle(capture, events)
            verification = _connect()
            try:
                parent_id, event_count, provider, variant = _capture_sql_rows(verification, uid)
                self.assertEqual(parent_id, stored.id)
                self.assertEqual(event_count, 2)
                self.assertEqual(provider, PROVIDER)
                self.assertEqual(variant, SOURCE_VARIANT)
            finally:
                verification.close()

            retry_capture, retry_events = record_capture_bundle(capture, events)
            self.assertEqual(retry_capture.id, stored.id)
            self.assertEqual(len(retry_events), 2)

            changed_base = _capture(uid, {"changed": True})
            changed_capture = replace(
                changed_base,
                response_fingerprint=build_response_fingerprint(changed_base, events),
            )
            with self.assertRaises(IdempotencyConflict):
                record_capture_bundle(changed_capture, events)

            with self.assertRaises(RuntimeError):
                record_capture_bundle(
                    _capture(TEST_UIDS[1]),
                    events,
                    connection_factory=lambda: _FailAfterParentConnection(_connect()),
                )
            failed_check = _connect()
            try:
                self.assertEqual(_capture_sql_rows(failed_check, TEST_UIDS[1])[0], 0)
            finally:
                failed_check.close()

            retry_capture, retry_events = record_capture_bundle(_capture(TEST_UIDS[1]), events)
            self.assertIsNotNone(retry_capture.id)
            self.assertEqual(len(retry_events), 2)

            barrier = threading.Barrier(2)
            results = []
            errors = []

            def race_call():
                try:
                    result = record_capture_bundle(
                        _capture(TEST_UIDS[2]),
                        events,
                        connection_factory=lambda: _BarrierConnection(_connect(), barrier),
                    )
                    results.append(result)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=race_call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=RACE_TIMEOUT_SECONDS + 5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertFalse(errors, errors)
            self.assertEqual(len(results), 2)
            self.assertEqual({result[0].id for result in results}, {results[0][0].id})
            self.assertTrue(all(len(result[1]) == 2 for result in results))

            final_check = _connect()
            try:
                parent_id, event_count, _, _ = _capture_sql_rows(final_check, TEST_UIDS[2])
                self.assertNotEqual(parent_id, 0)
                self.assertEqual(event_count, 2)
            finally:
                final_check.close()

            print("ACTUAL_REPOSITORY_FIRST_INSERT=PASS")
            print("IDENTICAL_RETRY=PASS")
            print("DIFFERENT_EVIDENCE_CONFLICT=PASS")
            print("ATOMIC_FAILURE_ROLLBACK=PASS")
            print("RETRY_AFTER_FAILURE=PASS")
            print("REAL_REPOSITORY_IDENTICAL_EVIDENCE_RACE=PASS")
            print("REAL_REPOSITORY_DIFFERENT_EVIDENCE_RACE=NOT_RUN")
        finally:
            _cleanup_reserved()


if __name__ == "__main__":
    unittest.main()
