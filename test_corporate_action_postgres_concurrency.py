"""Opt-in real PostgreSQL concurrency validation for corporate-action capture UIDs."""

from __future__ import annotations

import os
import threading
import time
import unittest
from datetime import datetime, timezone
from uuid import UUID


TEST_UID_X = UUID("00000000-0000-0000-0000-00000000a001")
TEST_UID_Y = UUID("00000000-0000-0000-0000-00000000a002")
TEST_UIDS = (TEST_UID_X, TEST_UID_Y)
RUN_FLAG = "CANSLIM_RUN_PG_INTEGRATION"
WAIT_TIMEOUT_SECONDS = 20.0
STATEMENT_TIMEOUT_MS = 15_000


def _connect():
    from database.db import get_connection

    return get_connection()


def _capture_values(capture_uid: UUID, digest_char: str) -> tuple:
    started = datetime(2025, 2, 1, tzinfo=timezone.utc)
    completed = datetime(2025, 2, 1, 0, 0, 1, tzinfo=timezone.utc)
    return (
        capture_uid,
        "YAHOO_FINANCE",
        "yfinance.splits",
        "legacy-split-capture-v1",
        datetime(2019, 2, 1, tzinfo=timezone.utc),
        started,
        completed,
        completed,
        "SUCCESS",
        "COMPLETE",
        "{}",
        "{}",
        "corporate-action-response-sha256-v2",
        digest_char * 64,
    )


INSERT_CAPTURE_SQL = """
    INSERT INTO public.corporate_action_captures (
        capture_uid,
        company_id,
        provider,
        source_variant,
        capture_contract_version,
        requested_window_start_at,
        capture_started_at,
        capture_completed_at,
        observed_at,
        acquisition_status,
        completeness_status,
        provider_metadata,
        error_metadata,
        response_fingerprint_version,
        response_fingerprint
    ) VALUES (
        %s,
        (SELECT id FROM public.companies WHERE ticker = 'AAPL'),
        %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s::jsonb, %s::jsonb, %s, %s
    )
    RETURNING id;
"""


class CorporateActionPostgresConcurrencyTests(unittest.TestCase):
    """These tests never run without an explicit real-DB opt-in."""

    @classmethod
    def setUpClass(cls):
        if os.getenv(RUN_FLAG) != "1":
            raise unittest.SkipTest(f"set {RUN_FLAG}=1 to run real PostgreSQL integration")

    def _new_connection(self, application_name: str):
        connection = _connect()
        try:
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (application_name,),
                )
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (f"{STATEMENT_TIMEOUT_MS}ms",),
                )
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _backend_pid(connection) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return int(cursor.fetchone()[0])

    @staticmethod
    def _preflight(connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            database_name, current_user = cursor.fetchone()
            if database_name != "canslim":
                raise AssertionError(f"unexpected database: {database_name}")
            if current_user != "canslim_app":
                raise AssertionError(f"unexpected current_user: {current_user}")

            cursor.execute(
                """
                SELECT to_regclass('public.corporate_action_captures'),
                       to_regclass('public.corporate_action_events')
                """
            )
            captures_table, events_table = cursor.fetchone()
            if captures_table is None or events_table is None:
                raise AssertionError("corporate-action tables are not installed")

    @staticmethod
    def _reserved_count(connection) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM public.corporate_action_captures
                WHERE capture_uid = ANY(%s::uuid[])
                """,
                ([str(uid) for uid in TEST_UIDS],),
            )
            return int(cursor.fetchone()[0])

    @staticmethod
    def _cleanup_reserved_uids() -> int:
        connection = _connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM public.corporate_action_events AS events
                    USING public.corporate_action_captures AS captures
                    WHERE captures.id = events.capture_id
                      AND captures.capture_uid = ANY(%s::uuid[])
                    """,
                    ([str(uid) for uid in TEST_UIDS],),
                )
                cursor.execute(
                    """
                    DELETE FROM public.corporate_action_captures
                    WHERE capture_uid = ANY(%s::uuid[])
                    """,
                    ([str(uid) for uid in TEST_UIDS],),
                )
            connection.commit()
            remaining = CorporateActionPostgresConcurrencyTests._reserved_count(connection)
            if remaining:
                raise AssertionError(f"reserved test rows remain: {remaining}")
            return remaining
        finally:
            connection.close()

    @staticmethod
    def _insert_capture(connection, capture_uid: UUID, digest_char: str) -> int:
        values = _capture_values(capture_uid, digest_char)
        with connection.cursor() as cursor:
            cursor.execute(INSERT_CAPTURE_SQL, values)
            return int(cursor.fetchone()[0])

    @staticmethod
    def _wait_for_block(connection, blocked_pid: int, blocking_pid: int) -> dict:
        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        query = """
            SELECT wait_event_type, wait_event, pg_blocking_pids(pid)
            FROM pg_stat_activity
            WHERE pid = %s
        """
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(query, (blocked_pid,))
                row = cursor.fetchone()
            if row and row[0] is not None and blocking_pid in (row[2] or []):
                return {
                    "wait_event_type": row[0],
                    "wait_event": row[1],
                    "blocking_pids": list(row[2] or []),
                }
            time.sleep(0.05)
        raise AssertionError("B was not observed waiting on A before timeout")

    def _run_race(self, capture_uid: UUID, digest_char: str, *, commit_a: bool):
        connection_a = self._new_connection("task-10b3c-A")
        connection_b = self._new_connection("task-10b3c-B")
        observer = self._new_connection("task-10b3c-observer")
        b_started = threading.Event()
        b_finished = threading.Event()
        result = {}

        pid_a = self._backend_pid(connection_a)
        pid_b = self._backend_pid(connection_b)
        if pid_a == pid_b:
            raise AssertionError("connections A and B do not have distinct backend PIDs")

        try:
            connection_a.execute("BEGIN")
            self._insert_capture(connection_a, capture_uid, digest_char)

            def contender():
                try:
                    connection_b.execute("BEGIN")
                    b_started.set()
                    self._insert_capture(connection_b, capture_uid, digest_char)
                    result["inserted"] = True
                except Exception as exc:  # psycopg exception inspected by caller
                    result["error"] = exc
                    result["sqlstate"] = getattr(exc, "sqlstate", None)
                finally:
                    b_finished.set()

            worker = threading.Thread(target=contender, name="corporate-action-contender")
            worker.start()
            if not b_started.wait(WAIT_TIMEOUT_SECONDS):
                raise AssertionError("B did not begin within timeout")

            observation = self._wait_for_block(observer, pid_b, pid_a)
            result["observation"] = observation

            if commit_a:
                connection_a.commit()
            else:
                connection_a.rollback()

            if not b_finished.wait(WAIT_TIMEOUT_SECONDS):
                raise AssertionError("B did not resume after A released the race")
            worker.join(timeout=1)

            if commit_a:
                if result.get("sqlstate") != "23505":
                    raise AssertionError(
                        f"expected SQLSTATE 23505, got {result.get('sqlstate')}"
                    )
                connection_b.rollback()
            else:
                if result.get("error") is not None or not result.get("inserted"):
                    raise AssertionError(f"B insert did not succeed: {result.get('error')}")
                connection_b.rollback()

            return {
                "pid_a": pid_a,
                "pid_b": pid_b,
                "observation": observation,
                "sqlstate": result.get("sqlstate"),
                "inserted_after_rollback": result.get("inserted", False),
            }
        finally:
            if not b_finished.is_set() and hasattr(connection_b, "cancel"):
                connection_b.cancel()
            for connection in (connection_a, connection_b, observer):
                try:
                    connection.rollback()
                except Exception:
                    pass
                connection.close()

    def test_capture_uid_concurrency_contract(self):
        preflight = _connect()
        try:
            self._preflight(preflight)
            preexisting = self._reserved_count(preflight)
            if preexisting:
                print(f"PREEXISTING_RESERVED_CAPTURE_ROWS={preexisting}")
        finally:
            preflight.close()

        self._cleanup_reserved_uids()
        try:
            rollback_result = self._run_race(TEST_UID_X, "a", commit_a=False)
            verification = _connect()
            try:
                with verification.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM public.corporate_action_captures WHERE capture_uid = %s",
                        (str(TEST_UID_X),),
                    )
                    rollback_zero = int(cursor.fetchone()[0]) == 0
            finally:
                verification.close()

            commit_result = self._run_race(TEST_UID_Y, "b", commit_a=True)
            self._cleanup_reserved_uids()
            final = _connect()
            try:
                with final.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT count(*)
                        FROM public.corporate_action_captures
                        WHERE capture_uid = ANY(%s::uuid[])
                        """,
                        ([str(uid) for uid in TEST_UIDS],),
                    )
                    final_zero = int(cursor.fetchone()[0]) == 0
            finally:
                final.close()

            print("DISTINCT_BACKENDS=PASS")
            print("ROLLBACK_CASE_BLOCKING=PASS")
            print("ROLLBACK_CASE_RELEASE_AND_INSERT=PASS")
            print(f"ROLLBACK_CASE_FINAL_ZERO={'PASS' if rollback_zero else 'FAIL'}")
            print("COMMIT_CASE_BLOCKING=PASS")
            print(
                "COMMIT_CASE_SQLSTATE_23505="
                + ("PASS" if commit_result["sqlstate"] == "23505" else "FAIL")
            )
            print("COMMIT_CASE_FINAL_ZERO=PASS")
            print(f"CLEANUP_FINAL_ZERO={'PASS' if final_zero else 'FAIL'}")
            print(f"ROLLBACK_OBSERVATION={rollback_result['observation']}")
            print(f"COMMIT_OBSERVATION={commit_result['observation']}")

            self.assertTrue(rollback_zero)
            self.assertTrue(final_zero)
        finally:
            self._cleanup_reserved_uids()


if __name__ == "__main__":
    unittest.main()
