import io
import runpy
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from database.backfill_normalized import backfill_company, main
from database.normalized_fundamentals import SEC_NORMALIZER_VERSION
from database.validate_fundamentals import (
    effective_sec_metric_period_gate,
    load_normalized_sec_records,
    run_validation,
)


def raw_row(raw_id, *, metric="EPS_DILUTED", period_end=date(2025, 6, 28),
            filed_date=date(2025, 8, 1), value=Decimal("1.57")):
    return {
        "id": raw_id,
        "company_id": 1,
        "source": "SEC",
        "source_variant": "sec.company_facts",
        "metric": metric,
        "period_start": date(2025, 3, 30),
        "period_end": period_end,
        "filed_date": filed_date,
        "fiscal_year": 2025,
        "fiscal_quarter": 3,
        "form_type": "10-Q",
        "value": value,
        "unit": {"EPS_DILUTED": "USD/shares", "REVENUE": "USD", "NET_INCOME": "USD", "DILUTED_SHARES": "shares"}[metric],
        "currency": "USD" if metric != "DILUTED_SHARES" else None,
        "xbrl_tag": "tag",
        "source_record_id": str(raw_id),
        "source_payload": {},
        "fetched_at": datetime(2025, 8, 2, tzinfo=timezone.utc),
        "created_at": datetime(2025, 8, 2, tzinfo=timezone.utc),
    }


class Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.staged = dict(self.connection.rows)
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.connection.rows = self.connection.staged
            self.connection.commits += 1
        else:
            self.connection.rollbacks += 1
        self.connection.staged = None
        return False


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.executed = []
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if "INSERT INTO fundamentals_normalized" in sql:
            raw_id = params[17]
            if raw_id == self.connection.fail_raw_id:
                raise RuntimeError("insert failure")
            key = (raw_id, params[18])
            if key in self.connection.staged:
                self._result = None
            else:
                identifier = len(self.connection.rows) + len(
                    set(self.connection.staged).difference(self.connection.rows)
                ) + 1
                self.connection.staged[key] = (identifier, *params)
                self._result = (identifier,)
        elif "FROM fundamentals_normalized" in sql:
            self._result = self.connection.staged[(params[0], params[1])]

    def fetchone(self):
        return self._result


class Connection:
    def __init__(self, fail_raw_id=None):
        self.rows = {}
        self.staged = None
        self.fail_raw_id = fail_raw_id
        self.commits = 0
        self.rollbacks = 0
        self.cursor_instance = Cursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return Transaction(self)

    def cursor(self):
        return self.cursor_instance


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            raw_row(1),
            raw_row(2, filed_date=date(2025, 8, 2), value=Decimal("1.58")),
            raw_row(3, metric="REVENUE", filed_date=None, value=Decimal("100")),
        ]

    def test_dry_run_normalizes_every_sec_raw_without_writing(self):
        with patch("database.backfill_normalized.load_raw_fundamentals", return_value=self.rows), \
             patch("database.backfill_normalized.get_connection") as connect:
            summary = backfill_company(1, "SEC", dry_run=True)

        self.assertEqual(summary, {
            "raw_read": 3, "normalized_candidates": 3, "inserted": 0,
            "existing": 0, "review": 1, "rejected": 0,
        })
        connect.assert_not_called()

    def test_fiscal_identity_is_shared_across_metrics_for_the_same_period(self):
        original = raw_row(1, metric="EPS_DILUTED")
        later_revenue_amendment = raw_row(
            2, metric="REVENUE", filed_date=date(2025, 8, 2), value=Decimal("100"),
        )
        later_revenue_amendment.update(fiscal_year=2026, fiscal_quarter=1)
        from database.normalized_fundamentals import normalize_sec_raw_row as normalizer

        with patch("database.backfill_normalized.load_raw_fundamentals", return_value=[original, later_revenue_amendment]), \
             patch("database.backfill_normalized.normalize_sec_raw_row", wraps=normalizer) as normalize:
            backfill_company(1, "SEC", dry_run=True)

        revenue_identity = normalize.call_args_list[1].args[1]
        self.assertEqual(revenue_identity, {"fiscal_year": 2025, "fiscal_quarter": 3})

    def test_apply_is_atomic_idempotent_and_never_mutates_legacy_quarterly(self):
        connection = Connection()
        with patch("database.backfill_normalized.load_raw_fundamentals", return_value=self.rows), \
             patch("database.backfill_normalized.get_connection", return_value=connection):
            first = backfill_company(1, "SEC", dry_run=False)
            second = backfill_company(1, "SEC", dry_run=False)

        self.assertEqual((first["inserted"], first["existing"]), (3, 0))
        self.assertEqual((second["inserted"], second["existing"]), (0, 3))
        self.assertEqual(connection.commits, 2)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(len(connection.rows), 3)
        sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed).lower()
        self.assertNotIn("fundamentals_quarterly", sql)
        self.assertNotIn("derived", sql)

    def test_failed_apply_rolls_back_the_entire_batch(self):
        connection = Connection(fail_raw_id=2)
        with patch("database.backfill_normalized.load_raw_fundamentals", return_value=self.rows), \
             patch("database.backfill_normalized.get_connection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "insert failure"):
                backfill_company(1, "SEC", dry_run=False)

        self.assertEqual(connection.rows, {})
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_non_sec_source_is_rejected_before_loading_raw_data(self):
        with patch("database.backfill_normalized.load_raw_fundamentals") as load:
            with self.assertRaisesRegex(ValueError, "SEC"):
                backfill_company(1, "YAHOO")
        load.assert_not_called()

    def test_unexpected_normalization_error_aborts_before_any_transaction(self):
        with patch("database.backfill_normalized.load_raw_fundamentals", return_value=[self.rows[0]]), \
             patch("database.backfill_normalized.normalize_sec_raw_row", side_effect=ValueError("bad raw contract")), \
             patch("database.backfill_normalized.get_connection") as connect:
            with self.assertRaisesRegex(ValueError, "bad raw contract"):
                backfill_company(1, "SEC", dry_run=False)
        connect.assert_not_called()

    def test_cli_requires_sec_and_an_explicit_execution_mode(self):
        output = io.StringIO()
        with patch("database.backfill_normalized._company_id_for_ticker", return_value=1), \
             patch("database.backfill_normalized.backfill_company", return_value={"raw_read": 135}) as run, \
             redirect_stdout(output):
            main(["AAPL", "--source", "SEC", "--dry-run"])
        run.assert_called_once_with(1, "SEC", dry_run=True)
        self.assertIn('"raw_read": 135', output.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["AAPL", "--source", "YAHOO", "--dry-run"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["AAPL", "--source", "SEC"])

    def test_script_help_runs_from_repository_root(self):
        result = subprocess.run(
            [sys.executable, "database/backfill_normalized.py", "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--source {SEC}", result.stdout)

    def test_validate_script_bootstraps_repository_root_before_runtime_imports(self):
        repository_root = str(Path(__file__).resolve().parent)
        script = Path(repository_root) / "database" / "validate_fundamentals.py"
        original_path = sys.path[:]
        try:
            sys.path[:] = [entry for entry in sys.path if entry not in {"", repository_root}]
            runpy.run_path(str(script), run_name="validate_bootstrap_probe")
            self.assertIn(repository_root, sys.path)
        finally:
            sys.path[:] = original_path


class EffectiveSecGateTests(unittest.TestCase):
    @staticmethod
    def gate_rows():
        rows = []
        for metric, count in (("EPS_DILUTED", 19), ("REVENUE", 19), ("NET_INCOME", 19), ("DILUTED_SHARES", 18)):
            for index in range(count):
                rows.append({
                    "metric": metric, "source": "SEC", "source_variant": "sec.company_facts",
                    "observation_kind": "REPORTED", "selection_eligibility": "ELIGIBLE",
                    "normalizer_version": SEC_NORMALIZER_VERSION, "source_period_end": date(2020 + index // 4, 3 + (index % 4) * 3, 28),
                    "filed_date": date(2021 + index // 4, 1, 1), "raw_id": index + 1,
                    "value": Decimal(str(index + 1)), "xbrl_tag": "tag",
                })
        return rows

    def test_gate_resolves_latest_filings_without_collapsing_normalized_evidence(self):
        rows = self.gate_rows()
        rows.append({**rows[0], "raw_id": 999, "filed_date": date(2030, 1, 1)})
        rows.append({**rows[1], "raw_id": 1000, "source": "DERIVED", "observation_kind": "DERIVED"})

        gate = effective_sec_metric_period_gate(rows)

        self.assertEqual(len(rows), 77)
        self.assertEqual(gate["effective_counts"], {
            "EPS_DILUTED": 19, "REVENUE": 19, "NET_INCOME": 19, "DILUTED_SHARES": 18,
        })
        self.assertEqual(gate["effective_total"], 75)
        self.assertTrue(gate["cardinality_passed"])
        self.assertEqual(gate["selected_by_metric"]["EPS_DILUTED"][0]["raw_id"], 999)

    def test_numeric_gate_fails_when_one_of_75_values_is_wrong(self):
        import pandas as pd

        rows = self.gate_rows()
        code_data = {
            "sec": {
                metric: pd.Series(
                    {
                        pd.Timestamp(row["source_period_end"]): float(row["value"])
                        for row in rows if row["metric"] == metric
                    }
                )
                for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")
            },
            "sec_tags": {metric: "tag" for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")},
        }
        first_period = min(row["source_period_end"] for row in rows if row["metric"] == "EPS_DILUTED")
        code_data["sec"]["EPS_DILUTED"].loc[pd.Timestamp(first_period)] = 999.0

        gate = effective_sec_metric_period_gate(rows, code_data=code_data)

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reproduced_counts"]["EPS_DILUTED"], 18)
        self.assertEqual(gate["reproduced_total"], 74)

    def test_loader_reads_normalized_sec_rows_by_ticker_without_writing(self):
        columns = [
            "metric", "source", "source_variant", "observation_kind", "selection_eligibility",
            "normalizer_version", "source_period_end", "filed_date", "raw_id", "value",
            "xbrl_tag", "fiscal_year", "fiscal_quarter",
        ]
        row = {**self.gate_rows()[0], "fiscal_year": 2025, "fiscal_quarter": 3}

        class ReadCursor:
            description = [type("Column", (), {"name": name}) for name in columns]

            def __init__(self):
                self.executed = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params):
                self.executed.append((sql, params))

            def fetchall(self):
                return [tuple(row[name] for name in columns)]

        class ReadConnection:
            def __init__(self):
                self.cursor_instance = ReadCursor()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return self.cursor_instance

        connection = ReadConnection()
        with patch("database.validate_fundamentals._get_connection", return_value=connection):
            records = load_normalized_sec_records("AAPL")

        self.assertEqual(records, [row])
        sql, params = connection.cursor_instance.executed[0]
        self.assertIn("fundamentals_normalized", sql)
        self.assertIn("fn.value", sql)
        self.assertIn("raw.xbrl_tag", sql)
        self.assertNotIn("INSERT", sql.upper())
        self.assertEqual(params, ("AAPL",))

    def test_run_validation_loads_normalized_gate_and_prints_metric_and_global_counts(self):
        from test_validate_fundamentals import baseline, fixture_code_data, fixture_postgres

        fixture = baseline()
        report = {**fixture["report"], "c_score_v1": fixture["c_score"]}
        code_data = fixture_code_data(fixture)
        normalized_rows = []
        raw_id = 1
        for metric, series in code_data["sec"].items():
            for period, value in series.items():
                normalized_rows.append({
                    "metric": metric, "source": "SEC", "source_variant": "sec.company_facts",
                    "observation_kind": "REPORTED", "selection_eligibility": "ELIGIBLE",
                    "normalizer_version": SEC_NORMALIZER_VERSION, "source_period_end": period.date(),
                    "filed_date": date(2026, 1, 1), "raw_id": raw_id,
                    "value": Decimal(str(value)), "xbrl_tag": code_data["sec_tags"].get(metric),
                    "fiscal_year": None, "fiscal_quarter": None,
                })
                raw_id += 1
        output = io.StringIO()
        with patch("database.validate_fundamentals.load_code_series", return_value=code_data), \
             patch("database.validate_fundamentals.load_postgres_records", return_value=fixture_postgres(fixture)), \
             patch("database.validate_fundamentals.load_normalized_sec_records", return_value=normalized_rows) as load, \
             patch.dict(sys.modules, {"fundamental_c": type("Fundamental", (), {"analyze_current_earnings": staticmethod(lambda _: report)})}), \
             redirect_stdout(output):
            run_validation("AAPL")

        load.assert_called_once_with("AAPL")
        text = output.getvalue()
        self.assertIn("EPS_DILUTED: 19/19", text)
        self.assertIn("REVENUE: 19/19", text)
        self.assertIn("NET_INCOME: 19/19", text)
        self.assertIn("DILUTED_SHARES: 18/18", text)
        self.assertIn("SEC_NORMALIZED_EFFECTIVE_METRIC_PERIOD=75/75 PASS", text)


if __name__ == "__main__":
    unittest.main()
