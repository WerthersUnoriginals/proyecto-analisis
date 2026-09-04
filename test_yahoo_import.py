import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pandas as pd

from database.fundamentals import _insert_raw_with_cursor
from database.yahoo_import import (
    TIMESERIES_VARIANT,
    YFINANCE_VARIANT,
    canonical_source_record_id,
    extract_yahoo_raw_facts,
    import_yahoo_fundamentals,
)


OBSERVED = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


class FakeStock:
    quarterly_income_stmt = pd.DataFrame(
        {
            pd.Timestamp("2025-09-30"): [Decimal("1.85"), Decimal("102466000000"), Decimal("27466000000"), Decimal("999")],
            pd.Timestamp("2025-06-30"): [Decimal("1.57"), Decimal("94036000000"), Decimal("23434000000"), Decimal("888")],
        },
        index=["Diluted EPS", "Total Revenue", "Net Income", "Diluted Average Shares"],
    )


def fake_timeseries(ticker, series_types, years=5):
    del ticker, years
    values = {
        "quarterlyDilutedEPS": [Decimal("1.85"), Decimal("1.57")],
        "quarterlyTotalRevenue": [Decimal("102466000000"), Decimal("94036000000")],
        "quarterlyNetIncome": [Decimal("27466000000"), Decimal("23434000000")],
    }
    dates = [pd.Timestamp("2025-09-30"), pd.Timestamp("2025-06-30")]
    return {series_type: pd.Series(dict(zip(dates, values[series_type])), dtype="object") for series_type in series_types}, None


def fake_clients():
    return {"timeseries_fetcher": fake_timeseries, "stock": FakeStock()}


class YahooExtractionTests(unittest.TestCase):
    def test_both_variants_and_three_metrics_are_explicit(self):
        rows = extract_yahoo_raw_facts("AAPL", 1, OBSERVED, clients=fake_clients())
        self.assertEqual({row["source_variant"] for row in rows}, {TIMESERIES_VARIANT, YFINANCE_VARIANT})
        self.assertEqual({row["metric"] for row in rows}, {"EPS_DILUTED", "REVENUE", "NET_INCOME"})
        self.assertEqual(len(rows), 12)
        self.assertNotIn("DILUTED_SHARES", {row["metric"] for row in rows})

    def test_raw_contract_preserves_source_dates_units_and_audit_payload(self):
        rows = extract_yahoo_raw_facts("AAPL", 1, OBSERVED, clients=fake_clients())
        by_key = {(row["source_variant"], row["metric"], row["period_end"]): row for row in rows}
        eps = by_key[(TIMESERIES_VARIANT, "EPS_DILUTED", date(2025, 9, 30))]
        revenue = by_key[(YFINANCE_VARIANT, "REVENUE", date(2025, 6, 30))]
        self.assertEqual((eps["source"], eps["unit"], eps["currency"]), ("YAHOO", "USD/shares", "USD"))
        self.assertEqual((revenue["unit"], revenue["currency"]), ("USD", "USD"))
        self.assertEqual(eps["fetched_at"], OBSERVED)
        self.assertIsNone(eps["filed_date"])
        self.assertIsNone(eps["source_available_at"])
        self.assertEqual(eps["source_payload"]["source_variant"], TIMESERIES_VARIANT)
        self.assertEqual(revenue["source_payload"]["source_variant"], YFINANCE_VARIANT)
        self.assertIn("provider_id", eps["source_payload"])

    def test_record_id_is_stable_canonical_and_changes_with_value(self):
        semantic = {"metric": "EPS_DILUTED", "period": "2025-09-30", "value": "1.85", "unit": "USD/shares", "currency": "USD"}
        reordered = dict(reversed(list(semantic.items())))
        first = canonical_source_record_id(TIMESERIES_VARIANT, "quarterlyDilutedEPS:2025-09-30", semantic)
        second = canonical_source_record_id(TIMESERIES_VARIANT, "quarterlyDilutedEPS:2025-09-30", reordered)
        corrected = canonical_source_record_id(TIMESERIES_VARIANT, "quarterlyDilutedEPS:2025-09-30", {**semantic, "value": "1.86"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, corrected)
        self.assertTrue(first.startswith("yahoo:"))

    def test_import_uses_one_batch_and_reports_variants(self):
        facts = extract_yahoo_raw_facts("AAPL", 1, OBSERVED, clients=fake_clients())
        with patch("database.yahoo_import.extract_yahoo_raw_facts", return_value=facts), patch("database.yahoo_import.insert_raw_fundamentals_batch", return_value=list(range(1, 13))) as insert:
            summary = import_yahoo_fundamentals("AAPL", 1, OBSERVED, clients=fake_clients())
        insert.assert_called_once_with(facts)
        self.assertEqual(summary["facts_found"], 12)
        self.assertEqual(summary["rows_resolved"], 12)
        self.assertEqual(summary["by_variant"], {TIMESERIES_VARIANT: 6, YFINANCE_VARIANT: 6})


class RawCursor:
    def __init__(self):
        self.rows = {}
        self.result = None
        self.next_id = 1
        self.statements = []

    def execute(self, sql, params):
        self.statements.append(sql)
        if "SELECT id" in sql:
            key = (params[0], params[1], params[2], params[3], params[4], params[5], params[6])
            self.result = (self.rows[key],) if key in self.rows else None
            return
        key = (params[0], params[1], params[2], params[4], params[12], params[5], params[13])
        self.rows[key] = self.next_id
        self.result = (self.next_id,)
        self.next_id += 1

    def fetchone(self):
        return self.result


class YahooRawIdempotencyTests(unittest.TestCase):
    def test_same_evidence_reuses_raw_and_correction_creates_new_row(self):
        facts = extract_yahoo_raw_facts("AAPL", 1, OBSERVED, clients=fake_clients())
        original = facts[0]
        corrected = {**original, "value": Decimal(str(original["value"])) + Decimal("0.01")}
        semantic = dict(corrected["source_payload"]["semantic_payload"])
        semantic["value"] = str(corrected["value"])
        corrected["source_payload"] = {**corrected["source_payload"], "semantic_payload": semantic}
        corrected["source_record_id"] = canonical_source_record_id(corrected["source_variant"], corrected["source_payload"]["provider_id"], semantic)
        cursor = RawCursor()
        first = _insert_raw_with_cursor(cursor, original)
        repeated = _insert_raw_with_cursor(cursor, original)
        correction = _insert_raw_with_cursor(cursor, corrected)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, correction)
        self.assertEqual(len(cursor.rows), 2)
        self.assertEqual(sum("INSERT INTO fundamentals_raw" in sql for sql in cursor.statements), 2)

    def test_semantic_precheck_is_scoped_to_yahoo(self):
        fact = extract_yahoo_raw_facts("AAPL", 1, OBSERVED, clients=fake_clients())[0]
        sec_fact = {**fact, "source": "SEC"}
        cursor = RawCursor()

        _insert_raw_with_cursor(cursor, sec_fact)
        _insert_raw_with_cursor(cursor, sec_fact)

        self.assertEqual(sum("INSERT INTO fundamentals_raw" in sql for sql in cursor.statements), 2)


if __name__ == "__main__":
    unittest.main()
