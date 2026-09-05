import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from database.backfill_semantics_v2 import build_v2_candidates, run
from test_normalized_fundamentals import sec_row, yahoo_row


class SemanticsV2BackfillTests(unittest.TestCase):
    def test_builds_one_append_only_v2_candidate_per_raw_row(self):
        sec_rows = [sec_row(id=1), sec_row(id=2, metric="REVENUE", unit="USD", xbrl_tag="RevenueFromContractWithCustomerExcludingAssessedTax")]
        yahoo_rows = [yahoo_row(id=3, source_payload={
            "source_variant": "yahoo.fundamentals_timeseries",
            "provider_id": "quarterlyDilutedEPS:2025-09-30",
        })]

        candidates = build_v2_candidates(sec_rows, yahoo_rows)

        self.assertEqual(len(candidates), 3)
        self.assertEqual({item.normalizer_version for item in candidates}, {
            "sec-normalized-v2", "yahoo-normalized-v2",
        })
        self.assertEqual({item.raw_id for item in candidates}, {1, 2, 3})

    def test_historical_yfinance_eps_is_preserved_but_ineligible(self):
        rows = [yahoo_row(id=index, period_end=date(2025, 9, 30), source_payload={
            "source_variant": "yfinance.quarterly_income_stmt",
            "provider_id": f"EPS_DILUTED:{index}",
        }) for index in range(10, 15)]

        candidates = build_v2_candidates([], rows)

        self.assertEqual(len(candidates), 5)
        self.assertTrue(all(item.metric == "EPS_UNSPECIFIED" for item in candidates))
        self.assertTrue(all(item.selection_eligibility == "INELIGIBLE" for item in candidates))
        self.assertTrue(all(item.source_metric_name == "legacy.unknown" for item in candidates))

    def test_future_yfinance_basic_is_not_diluted(self):
        row = yahoo_row(metric="EPS_BASIC", source_payload={
            "source_variant": "yfinance.quarterly_income_stmt",
            "source_metric_name": "Basic EPS",
            "source_unit": "USD/shares",
            "source_scale_factor": "1",
        })

        item = build_v2_candidates([], [row])[0]

        self.assertEqual(item.metric, "EPS_BASIC")
        self.assertNotEqual(item.metric, "EPS_DILUTED")
        self.assertEqual(item.value, Decimal("1.85") * item.source_scale_factor)

    def test_apply_uses_one_connection_and_one_transaction_and_preserves_v1_ids(self):
        class Cursor:
            description = ()
            def __init__(self):
                self.executed = []
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql, params=()): self.executed.append((sql, params))
            def fetchone(self): return ([11, 12],)
        class Transaction:
            def __init__(self): self.exited_with = None
            def __enter__(self): return self
            def __exit__(self, exc_type, *_): self.exited_with = exc_type; return False
        class Connection:
            def __init__(self): self.cursor_value = Cursor(); self.tx = Transaction()
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): return self.cursor_value
            def transaction(self): return self.tx
        connection = Connection()
        candidate = build_v2_candidates([sec_row(id=1)], [])[0]
        with patch("database.backfill_semantics_v2.get_connection", return_value=connection), \
             patch("database.backfill_semantics_v2._load_raw", side_effect=([sec_row(id=1)], [])), \
             patch("database.backfill_semantics_v2._load_sec_calendar", return_value=[]), \
             patch("database.backfill_semantics_v2._insert_batch_in_transaction", return_value=(1, 0)), \
             patch("database.backfill_semantics_v2._database_gates", return_value={"ok": True}) as gates:
            result = run(1, dry_run=False)
        self.assertEqual(result["inserted"], 1)
        self.assertIsNone(connection.tx.exited_with)
        gates.assert_called_once_with(connection.cursor_value, 1, (11, 12))

    def test_second_apply_reports_only_existing_rows(self):
        class Cursor:
            description = ()
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, *_): pass
            def fetchone(self): return ([11],)
        class Context:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): return Cursor()
            def transaction(self): return self
        candidates = build_v2_candidates([sec_row(id=1)], [])
        with patch("database.backfill_semantics_v2.get_connection", return_value=Context()), \
             patch("database.backfill_semantics_v2._load_raw", side_effect=([sec_row(id=1)], [])), \
             patch("database.backfill_semantics_v2._load_sec_calendar", return_value=[]), \
             patch("database.backfill_semantics_v2._insert_batch_in_transaction", return_value=(0, 1)), \
             patch("database.backfill_semantics_v2._database_gates", return_value={"ok": True}):
            result = run(1, dry_run=False)
        self.assertEqual((result["inserted"], result["existing"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
