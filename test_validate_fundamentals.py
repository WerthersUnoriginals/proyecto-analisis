import json
import hashlib
import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from database.validate_fundamentals import (
    _hybrid_records,
    _yahoo_source_variants,
    build_validation_result,
    c_score_inputs,
    compare_hybrid_records,
    compare_sec_records,
    numeric_status,
    q4_coverage,
    reproduced_count,
    run_validation,
)


BASELINE_PATH = Path("fixtures/aapl_c_v26_baseline.json")
BASELINE_SHA256 = "0b4a2d63ea20c9bb9de2f724c3f57d76dba047018841c920a9aca3b7dcdf96ee"
YAHOO_VARIANTS = {
    "yahoo.fundamentals_timeseries",
    "yfinance.quarterly_income_stmt",
}


def point(period, value, source="SEC", **extra):
    return {"period": date.fromisoformat(period), "value": value, "source": source, **extra}


def fake_code():
    import pandas as pd

    eps = pd.Series({pd.Timestamp("2024-06-29"): 1.0, pd.Timestamp("2025-06-28"): 2.0})
    revenue = pd.Series(
        {pd.Timestamp("2024-06-29"): 100.0, pd.Timestamp("2025-06-28"): 200.0}
    )
    empty = pd.Series(dtype="float64")
    return {
        "sec": {"EPS_DILUTED": eps, "REVENUE": revenue, "NET_INCOME": empty, "DILUTED_SHARES": empty},
        "sec_tags": {},
        "yahoo": {"EPS_DILUTED": empty, "REVENUE": empty, "NET_INCOME": empty},
        "hybrid": {
            "EPS_DILUTED": [point("2024-06-29", 1.0), point("2025-06-28", 2.0)],
            "REVENUE": [point("2024-06-29", 100.0), point("2025-06-28", 200.0)],
            "NET_INCOME": [],
        },
        "report": {"c_score_v1": {"normalized_score": 50.0}},
    }


def fake_pg():
    return {
        "EPS_DILUTED": [point("2024-06-29", 1.0), point("2025-06-28", 2.0)],
        "REVENUE": [point("2024-06-29", 100.0), point("2025-06-28", 200.0)],
        "NET_INCOME": [],
        "DILUTED_SHARES": [],
    }


def baseline():
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _date_string(value):
    return value.isoformat() if isinstance(value, date) else value


def _json_dates(value):
    if isinstance(value, dict):
        return {key: _json_dates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_dates(item) for item in value]
    return _date_string(value)


def fixture_code_data(fixture):
    import pandas as pd

    columns = fixture["sec_columns"]
    sec_rows = [dict(zip(columns, row)) for row in fixture["sec_observations"]]
    sec = {
        metric: pd.Series(
            {
                pd.Timestamp(row["period"]): row["value"]
                for row in sec_rows
                if row["metric"] == metric
            },
            dtype="float64",
        )
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")
    }
    hybrid_rows = [dict(zip(fixture["hybrid_columns"], row)) for row in fixture["hybrid_observations"]]
    yahoo = {
        metric: pd.Series(
            {
                pd.Timestamp(row["period"]): row["value"]
                for row in hybrid_rows
                if row["metric"] == metric and row["source"] == "YAHOO"
            },
            dtype="float64",
        )
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME")
    }
    hybrid = {
        metric: [
            {
                "period": date.fromisoformat(row["period"]),
                "value": row["value"],
                "source": row["source"],
                "source_variant": row["source_variant"],
            }
            for row in hybrid_rows
            if row["metric"] == metric
        ]
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME")
    }
    return {
        "sec": sec,
        "sec_tags": fixture["report"]["sec_tags"],
        "yahoo": yahoo,
        "hybrid": hybrid,
    }


def fixture_postgres(fixture):
    columns = fixture["sec_columns"]
    rows = [dict(zip(columns, row)) for row in fixture["sec_observations"]]
    return {
        metric: [
            {
                "period": date.fromisoformat(row["postgres_period"]),
                "value": row["postgres_value"],
                "source": "SEC",
                "tag": row["tag"],
                "filed_date": date.fromisoformat(row["filed_date"]),
                "raw_id": row["raw_id"],
                "fiscal_year": row["fiscal_year"],
                "fiscal_quarter": row["fiscal_quarter"],
            }
            for row in rows
            if row["metric"] == metric
        ]
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")
    }


def normalized_result(result):
    sec = [
        [
            metric,
            _date_string(row["period_code"]),
            row["value_code"],
            row["source_code"],
            row["tag_code"],
            _date_string(row["filed_date"]),
            row["source_raw_id"],
            row["fiscal_year"],
            row["fiscal_quarter"],
            _date_string(row["period_postgres"]),
            row["value_postgres"],
            row["status"],
        ]
        for metric, rows in result["sec"].items()
        for row in rows
    ]
    hybrid = [
        [
            metric,
            _date_string(row["period_code"]),
            row["value_code"],
            row["source_code"],
            row["source_variant_code"],
            _date_string(row["period_postgres"]),
            row["value_postgres"],
            row["status"],
        ]
        for metric, rows in result["hybrid"].items()
        for row in rows
    ]
    return {
        "sec_observations": sec,
        "hybrid_observations": hybrid,
        "q4": _json_dates(result["q4"]),
        "inputs": _json_dates(result["inputs"]),
        "report": result["report"],
        "c_score": result["c_score"],
    }


class StructuredBaselineTests(unittest.TestCase):
    def test_result_exposes_all_regression_gates(self):
        result = build_validation_result("AAPL", code_data=fake_code(), postgres=fake_pg())
        self.assertEqual(set(result), {"sec", "hybrid", "q4", "inputs", "report", "c_score"})

    def test_fixture_names_the_three_yahoo_q4_fallbacks(self):
        fixture = baseline()
        self.assertEqual(
            {(row["metric"], row["period"]) for row in fixture["yahoo_fallbacks"]},
            {("EPS_DILUTED", "2025-09-30"), ("REVENUE", "2025-09-30"),
             ("NET_INCOME", "2025-09-30")},
        )

    def test_fixture_is_the_exact_aapl_contract_snapshot(self):
        fixture = baseline()
        self.assertEqual(
            hashlib.sha256(BASELINE_PATH.read_bytes()).hexdigest(),
            BASELINE_SHA256,
        )
        self.assertEqual(fixture["counts"], {
            "sec_effective": 75,
            "hybrid_effective": 60,
            "postgres_backed_hybrid": 57,
            "yahoo_fallbacks": 3,
            "input_groups": 10,
        })
        sec = [dict(zip(fixture["sec_columns"], row)) for row in fixture["sec_observations"]]
        self.assertEqual(
            {metric: sum(row["metric"] == metric for row in sec) for metric in (
                "EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES"
            )},
            {"EPS_DILUTED": 19, "REVENUE": 19, "NET_INCOME": 19, "DILUTED_SHARES": 18},
        )
        self.assertTrue(all(row["source"] == "SEC" and row["status"] == "EXACT_MATCH" for row in sec))
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES"):
            periods = [row["period"] for row in sec if row["metric"] == metric]
            self.assertEqual(periods, sorted(periods))
        hybrid = [dict(zip(fixture["hybrid_columns"], row)) for row in fixture["hybrid_observations"]]
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME"):
            periods = [row["period"] for row in hybrid if row["metric"] == metric]
            self.assertEqual(periods, sorted(periods))
        self.assertEqual(sum(row["status"] != "YAHOO_FALLBACK" for row in hybrid), 57)
        fallbacks = [row for row in hybrid if row["status"] == "YAHOO_FALLBACK"]
        self.assertEqual(len(fallbacks), 3)
        self.assertTrue(all(row["source"] == "YAHOO" for row in fallbacks))
        self.assertTrue(all(row["postgres_period"] is None and row["postgres_value"] is None for row in fallbacks))
        self.assertTrue(all(row["source_variant"] in YAHOO_VARIANTS for row in fallbacks))
        self.assertEqual(sum(row["reproducible"] == "SÍ" for row in fixture["inputs"]), 10)
        self.assertEqual(fixture["report"]["latest_eps_source"], "SEC")
        self.assertEqual(fixture["c_score"]["normalized_score"], 62.69)
        self.assertEqual(fixture["c_score"]["class"], "ACCEPTABLE")

    def test_result_matches_the_serialized_baseline_from_explicit_snapshot(self):
        fixture = baseline()
        report = {**fixture["report"], "c_score_v1": fixture["c_score"]}
        result = build_validation_result(
            "AAPL",
            code_data=fixture_code_data(fixture),
            postgres=fixture_postgres(fixture),
            report=report,
        )
        expected = {
            "sec_observations": fixture["sec_observations"],
            "hybrid_observations": fixture["hybrid_observations"],
            "q4": fixture["q4"],
            "inputs": fixture["inputs"],
            "report": fixture["report"],
            "c_score": fixture["c_score"],
        }
        self.assertEqual(normalized_result(result), expected)
        repeated = build_validation_result(
            "AAPL",
            code_data=fixture_code_data(fixture),
            postgres=fixture_postgres(fixture),
            report=report,
        )
        self.assertEqual(
            json.dumps(normalized_result(result), ensure_ascii=True, sort_keys=True),
            json.dumps(normalized_result(repeated), ensure_ascii=True, sort_keys=True),
        )

    def test_explicit_report_prevents_analyzer_refetch(self):
        code_data = fake_code()
        report = code_data.pop("report")
        fake_fundamental = types.SimpleNamespace(
            analyze_current_earnings=lambda ticker: (_ for _ in ()).throw(AssertionError("refetch"))
        )
        with patch.dict(sys.modules, {"fundamental_c": fake_fundamental}):
            result = build_validation_result("AAPL", code_data=code_data, postgres=fake_pg(), report=report)
        self.assertEqual(result["c_score"], report["c_score_v1"])

    def test_one_argument_api_uses_the_validation_snapshot_constructor(self):
        report = fake_code()["report"]
        snapshot = {"code_data": fake_code(), "postgres": fake_pg(), "report": report}
        with patch("database.validate_fundamentals.load_validation_snapshot", return_value=snapshot) as capture:
            result = build_validation_result("aapl")
        capture.assert_called_once_with("AAPL")
        self.assertEqual(set(result), {"sec", "hybrid", "q4", "inputs", "report", "c_score"})

    def test_default_builder_and_runner_acquire_each_legacy_view_once(self):
        fixture = baseline()
        report = {**fixture["report"], "c_score_v1": fixture["c_score"]}
        analyzer = Mock(return_value=report)

        with patch("database.validate_fundamentals.load_code_series", return_value=fixture_code_data(fixture)) as code, \
             patch("database.validate_fundamentals.load_postgres_records", return_value=fixture_postgres(fixture)) as postgres, \
             patch.dict(sys.modules, {"fundamental_c": types.SimpleNamespace(analyze_current_earnings=analyzer)}):
            build_validation_result("AAPL")
        code.assert_called_once_with("AAPL")
        postgres.assert_called_once_with("AAPL")
        analyzer.assert_called_once_with("AAPL")

        analyzer = Mock(return_value=report)
        with patch("database.validate_fundamentals.load_code_series", return_value=fixture_code_data(fixture)) as code, \
             patch("database.validate_fundamentals.load_postgres_records", return_value=fixture_postgres(fixture)) as postgres, \
             patch.dict(sys.modules, {"fundamental_c": types.SimpleNamespace(analyze_current_earnings=analyzer)}), \
             redirect_stdout(io.StringIO()):
            run_validation("AAPL")
        code.assert_called_once_with("AAPL")
        postgres.assert_called_once_with("AAPL")
        analyzer.assert_called_once_with("AAPL")

    def test_runner_prints_source_errors_from_its_snapshot(self):
        fixture = baseline()
        report = {**fixture["report"], "c_score_v1": fixture["c_score"]}
        code_data = fixture_code_data(fixture)
        code_data["errors"] = {"sec": "fuente no disponible"}
        analyzer = Mock(return_value=report)
        output = io.StringIO()
        with patch("database.validate_fundamentals.load_code_series", return_value=code_data), \
             patch("database.validate_fundamentals.load_postgres_records", return_value=fixture_postgres(fixture)), \
             patch.dict(sys.modules, {"fundamental_c": types.SimpleNamespace(analyze_current_earnings=analyzer)}), \
             redirect_stdout(output):
            run_validation("AAPL")
        self.assertIn("Errores/fallbacks de fuentes: {'sec': 'fuente no disponible'}", output.getvalue())
        analyzer.assert_called_once_with("AAPL")

    def test_yahoo_variants_are_attributed_before_merge_and_preserved_in_hybrid(self):
        import pandas as pd

        primary = pd.Series({pd.Timestamp("2025-09-30"): 1.85})
        fallback = pd.Series({pd.Timestamp("2025-09-27"): 1.84, pd.Timestamp("2025-12-27"): 2.84})
        variants = _yahoo_source_variants(primary, fallback)
        self.assertEqual(variants[pd.Timestamp("2025-09-30")], "yahoo.fundamentals_timeseries")
        self.assertNotIn(pd.Timestamp("2025-09-27"), variants)
        self.assertEqual(variants[pd.Timestamp("2025-12-27")], "yfinance.quarterly_income_stmt")
        rows = _hybrid_records(
            pd.Series(dtype="float64"),
            pd.Series({pd.Timestamp("2025-12-27"): 2.84}),
            yahoo_variants=variants,
        )
        self.assertEqual(rows[0]["source_variant"], "yfinance.quarterly_income_stmt")


class NumericToleranceTests(unittest.TestCase):
    def test_identical_eps_is_exact_match(self):
        self.assertEqual(numeric_status("EPS_DILUTED", 2.02, 2.02)[0], "EXACT_MATCH")

    def test_small_eps_representation_difference_uses_tolerance(self):
        status, absolute, relative = numeric_status("EPS_DILUTED", 2.020000004, 2.02)
        self.assertEqual(status, "NUMERIC_TOLERANCE")
        self.assertAlmostEqual(absolute, 0.000000004)
        self.assertGreater(relative, 0)

    def test_material_monetary_difference_is_other(self):
        self.assertEqual(numeric_status("REVENUE", 100_000_000, 100_001_000)[0], "OTHER")


class ExactSecComparisonTests(unittest.TestCase):
    def test_matches_sec_only_on_exact_period_end(self):
        code = [point("2026-06-27", 2.02)]
        postgres = [point("2026-06-27", 2.02, tag="EarningsPerShareDiluted")]
        rows = compare_sec_records("EPS_DILUTED", code, postgres)
        self.assertEqual(rows[0]["status"], "EXACT_MATCH")
        self.assertEqual(rows[0]["period_code"], date(2026, 6, 27))
        self.assertEqual(rows[0]["period_postgres"], date(2026, 6, 27))

    def test_reports_both_sides_when_dates_do_not_match_exactly(self):
        code = [point("2026-06-30", 2.02)]
        postgres = [point("2026-06-27", 2.02)]
        rows = compare_sec_records("EPS_DILUTED", code, postgres)
        self.assertEqual([row["status"] for row in rows], ["MISSING_IN_POSTGRES", "MISSING_IN_CODE"])


class HybridComparisonTests(unittest.TestCase):
    def test_aligns_yahoo_to_postgres_within_35_days(self):
        hybrid = [point("2026-06-30", 2.02, source="YAHOO")]
        postgres = [point("2026-06-27", 2.02)]
        rows = compare_hybrid_records("EPS_DILUTED", hybrid, postgres)
        self.assertEqual(rows[0]["status"], "DATE_ALIGNMENT")
        self.assertEqual(rows[0]["period_postgres"], date(2026, 6, 27))

    def test_classifies_unmatched_yahoo_period_as_fallback(self):
        hybrid = [point("2025-09-30", 1.85, source="YAHOO")]
        postgres = [point("2025-06-28", 1.57)]
        rows = compare_hybrid_records("EPS_DILUTED", hybrid, postgres)
        self.assertEqual(rows[0]["status"], "YAHOO_FALLBACK")
        self.assertIsNone(rows[0]["period_postgres"])

    def test_reports_postgres_period_missing_from_hybrid(self):
        rows = compare_hybrid_records(
            "REVENUE",
            [point("2026-06-27", 109_417_000_000)],
            [point("2026-06-27", 109_417_000_000), point("2026-03-28", 111_184_000_000)],
        )
        self.assertEqual(rows[-1]["status"], "MISSING_IN_CODE")
        self.assertEqual(rows[-1]["period_postgres"], date(2026, 3, 28))

    def test_material_difference_is_not_counted_as_reproduced(self):
        rows = compare_hybrid_records(
            "REVENUE",
            [point("2026-06-27", 109_417_000_000)],
            [point("2026-06-27", 1)],
        )
        self.assertEqual(rows[0]["status"], "OTHER")
        self.assertEqual(reproduced_count(rows), 0)


class Q4CoverageTests(unittest.TestCase):
    def test_q4_yoy_requires_a_comparable_q4_not_growth_elsewhere(self):
        import pandas as pd

        sec = pd.Series(
            {
                pd.Timestamp("2020-03-28"): 1.0,
                pd.Timestamp("2020-09-26"): 2.0,
                pd.Timestamp("2021-03-27"): 1.5,
            }
        )
        empty = pd.Series(dtype="float64")
        code_data = {
            "sec": {"EPS_DILUTED": sec, "REVENUE": empty, "NET_INCOME": empty, "DILUTED_SHARES": empty},
            "yahoo": {"EPS_DILUTED": empty, "REVENUE": empty, "NET_INCOME": empty},
            "hybrid": {
                "EPS_DILUTED": [point("2020-09-26", 2.0)],
                "REVENUE": [],
                "NET_INCOME": [],
            },
        }
        postgres = {
            "EPS_DILUTED": [
                point("2020-09-26", 2.0, fiscal_year=2020, fiscal_quarter=4)
            ],
            "REVENUE": [],
            "NET_INCOME": [],
            "DILUTED_SHARES": [],
        }
        rows = q4_coverage(code_data, postgres)
        fy2020 = next(row for row in rows if row["fiscal_year"] == 2020 and row["metric"] == "EPS_DILUTED")
        self.assertFalse(fy2020["yoy"])

    def test_q4_coverage_is_reported_for_every_metric_and_year(self):
        import pandas as pd

        empty = pd.Series(dtype="float64")
        code_data = {
            "sec": {metric: empty for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")},
            "yahoo": {metric: empty for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME")},
            "hybrid": {metric: [] for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME")},
        }
        postgres = {metric: [] for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME", "DILUTED_SHARES")}
        rows = q4_coverage(code_data, postgres)
        self.assertEqual(len(rows), 24)
        shares_2020 = next(
            row for row in rows if row["fiscal_year"] == 2020 and row["metric"] == "DILUTED_SHARES"
        )
        self.assertEqual(shares_2020["yahoo"], "N/A")
        self.assertEqual(shares_2020["hybrid"], "N/A")


class CScoreReproducibilityTests(unittest.TestCase):
    def test_period_presence_does_not_hide_a_numeric_difference(self):
        import pandas as pd

        eps = pd.Series({pd.Timestamp("2025-06-28"): 1.57, pd.Timestamp("2026-06-27"): 2.02})
        revenue = pd.Series(
            {pd.Timestamp("2025-06-28"): 94_036_000_000, pd.Timestamp("2026-06-27"): 109_417_000_000}
        )
        empty = pd.Series(dtype="float64")
        code_data = {
            "sec": {"EPS_DILUTED": eps, "REVENUE": revenue},
            "sec_tags": {},
            "yahoo": {"EPS_DILUTED": empty, "REVENUE": empty},
            "hybrid": {"EPS_DILUTED": [point("2025-06-28", 1.57), point("2026-06-27", 2.02)]},
        }
        postgres = {
            "EPS_DILUTED": [point("2025-06-28", 1.57), point("2026-06-27", 9.99)],
            "REVENUE": [point("2025-06-28", 94_036_000_000), point("2026-06-27", 109_417_000_000)],
        }
        inputs = {item["input"]: item for item in c_score_inputs(code_data, postgres)}
        self.assertEqual(inputs["latest_eps_yoy_pct"]["reproducible"], "NO")


if __name__ == "__main__":
    unittest.main()
