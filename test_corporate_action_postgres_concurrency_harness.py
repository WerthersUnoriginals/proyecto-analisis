import unittest
import threading
from unittest.mock import patch

import test_corporate_action_postgres_concurrency as harness
import test_corporate_action_postgres_repository_integration as repository_harness


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        self.connection.executed.append((statement, params))
        if self.connection.fail_on_execute == len(self.connection.executed):
            raise RuntimeError("initialization failure")


class FakeConnection:
    def __init__(self, fail_on_execute=None):
        self.fail_on_execute = fail_on_execute
        self.executed = []
        self.autocommit = None
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class DelegatingCursor:
    rowcount = 2
    description = ("value",)

    def __init__(self):
        self.executed = []
        self.closed = False
        self.connection = object()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def execute(self, statement, params=None):
        self.executed.append((statement, params))

    def executemany(self, statement, params):
        self.executed.extend((statement, item) for item in params)

    def fetchone(self):
        return ("one",)

    def fetchall(self):
        return [("one",), ("two",)]

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        self.closed = True


class HarnessInitializationTests(unittest.TestCase):
    def test_parameter_safe_initialization_returns_open_connection(self):
        connection = FakeConnection()
        test_case = harness.CorporateActionPostgresConcurrencyTests()

        with patch.object(harness, "_connect", return_value=connection):
            result = test_case._new_connection("task-test")

        self.assertIs(result, connection)
        self.assertFalse(connection.closed)
        self.assertTrue(connection.committed)
        self.assertEqual(connection.executed[0][0], "SELECT set_config('application_name', %s, false)")
        self.assertEqual(connection.executed[0][1], ("task-test",))
        self.assertEqual(connection.executed[1][0], "SELECT set_config('statement_timeout', %s, false)")
        self.assertNotIn("SET application_name = %s", connection.executed[0][0])
        self.assertNotIn("SET statement_timeout = %s", connection.executed[1][0])

    def test_initialization_failure_closes_open_connection(self):
        connection = FakeConnection(fail_on_execute=2)
        test_case = harness.CorporateActionPostgresConcurrencyTests()

        with patch.object(harness, "_connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "initialization failure"):
                test_case._new_connection("task-test")

        self.assertTrue(connection.closed)
        self.assertFalse(connection.committed)

    def test_real_database_opt_in_gate_remains_explicit(self):
        self.assertEqual(harness.RUN_FLAG, "CANSLIM_RUN_PG_INTEGRATION")
        self.assertEqual(str(harness.TEST_UID_X), "00000000-0000-0000-0000-00000000a001")
        self.assertEqual(str(harness.TEST_UID_Y), "00000000-0000-0000-0000-00000000a002")

    def test_barrier_cursor_delegates_cursor_protocol_and_preserves_barrier(self):
        underlying = DelegatingCursor()
        barrier = threading.Barrier(1)
        cursor = repository_harness._BarrierCursor(underlying, barrier)

        with cursor as active:
            active.execute("SELECT 1")
            self.assertEqual(active.fetchall(), [("one",), ("two",)])
            self.assertEqual(list(active), [("one",), ("two",)])
            self.assertEqual(active.rowcount, 2)
            self.assertEqual(active.description, ("value",))
            active.execute("INSERT INTO corporate_action_captures VALUES (%s)", (1,))

        self.assertTrue(underlying.closed)
        self.assertEqual(len(underlying.executed), 2)


if __name__ == "__main__":
    unittest.main()
