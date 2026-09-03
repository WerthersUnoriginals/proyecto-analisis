import unittest
from datetime import date

from database.validate_fundamentals import (
    c_score_inputs,
    compare_hybrid_records,
    compare_sec_records,
    numeric_status,
    q4_coverage,
    reproduced_count,
)


def point(period, value, source="SEC", **extra):
    return {"period": date.fromisoformat(period), "value": value, "source": source, **extra}


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
