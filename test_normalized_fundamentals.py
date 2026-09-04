import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from database.normalized_fundamentals import (
    NormalizedObservation,
    SEC_NORMALIZER_VERSION,
    _original_sec_fiscal_identity,
    normalize_sec_raw_row,
    insert_normalized_batch,
    load_raw_fundamentals,
)
from database.fundamentals import _insert_raw_with_cursor
from database.sec_import import import_sec_fundamentals


def sec_row(**overrides):
    row = {
        "id": 77,
        "company_id": 1,
        "source": "SEC",
        "source_variant": "sec.company_facts",
        "metric": "EPS_DILUTED",
        "period_start": date(2025, 3, 30),
        "period_end": date(2025, 6, 28),
        "filed_date": date(2025, 8, 1),
        "fiscal_year": 2025,
        "fiscal_quarter": 3,
        "form_type": "10-Q",
        "value": Decimal("1.57"),
        "unit": "USD/shares",
        "currency": "USD",
        "xbrl_tag": "EarningsPerShareDiluted",
        "source_record_id": "0000320193-25-000079",
        "source_payload": {"unit": "USD/shares"},
        "fetched_at": datetime(2025, 8, 2, 9, 30, tzinfo=timezone.utc),
        "created_at": datetime(2025, 8, 2, 9, 30, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


class ScriptedCursor:
    def __init__(self, responses, description=()):
        self.responses = iter(responses)
        self.description = description
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return next(self.responses)

    def fetchall(self):
        return next(self.responses)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ScriptedConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class SecNormalizedTests(unittest.TestCase):
    def test_sec_preserves_raw_dates_variant_and_value(self):
        raw = sec_row()
        item = normalize_sec_raw_row(
            raw, fiscal_identity={"fiscal_year": 2025, "fiscal_quarter": 3}
        )
        self.assertIsInstance(item, NormalizedObservation)
        self.assertEqual(item.source_variant, "sec.company_facts")
        self.assertEqual(item.value, raw["value"])
        self.assertEqual(item.unit, raw["unit"])
        self.assertEqual(item.source_period_start, raw["period_start"])
        self.assertEqual(item.source_period_end, raw["period_end"])
        self.assertEqual(item.series_date, item.source_period_end)
        self.assertEqual(item.filed_date, raw["filed_date"])
        self.assertEqual(item.observed_at, raw["fetched_at"])
        self.assertIsNone(item.source_available_at)
        self.assertEqual(item.intrinsic_quality_status, "OK")
        self.assertEqual(item.selection_eligibility, "ELIGIBLE")
        self.assertEqual(item.normalizer_version, SEC_NORMALIZER_VERSION)

    def test_sec_uses_provided_original_fiscal_identity_without_mutating_raw(self):
        raw = sec_row(fiscal_year=2026, fiscal_quarter=1)
        item = normalize_sec_raw_row(
            raw,
            fiscal_identity={
                "fiscal_year": 2025,
                "fiscal_quarter": 4,
                "period_start": date(2025, 6, 29),
                "form_type": "10-K",
            },
        )
        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (2025, 4))
        self.assertEqual(item.source_period_start, raw["period_start"])
        self.assertEqual(raw["fiscal_year"], 2026)

    def test_dataclass_is_frozen(self):
        item = normalize_sec_raw_row(sec_row(), {"fiscal_year": 2025, "fiscal_quarter": 3})
        with self.assertRaisesRegex(Exception, "cannot assign to field"):
            item.value = Decimal("2.00")

    def test_missing_sec_filing_is_review_and_ineligible(self):
        item = normalize_sec_raw_row(
            sec_row(filed_date=None), {"fiscal_year": 2025, "fiscal_quarter": 3}
        )
        self.assertEqual(item.intrinsic_quality_status, "REVIEW_REQUIRED")
        self.assertEqual(item.selection_eligibility, "INELIGIBLE")

    def test_incomplete_fiscal_identity_is_review_and_ineligible(self):
        item = normalize_sec_raw_row(sec_row(), {"fiscal_year": 2025, "fiscal_quarter": None})
        self.assertEqual(item.intrinsic_quality_status, "REVIEW_REQUIRED")
        self.assertEqual(item.selection_eligibility, "INELIGIBLE")
        self.assertIn("MISSING_FISCAL_QUARTER", item.intrinsic_quality_reasons)

    def test_sec_rejects_a_source_variant_other_than_the_raw_contract(self):
        with self.assertRaisesRegex(ValueError, "source_variant"):
            normalize_sec_raw_row(sec_row(source_variant="sec.other"), {"fiscal_year": 2025, "fiscal_quarter": 3})

    def test_dataclass_enforces_sec_filing_and_derived_ineligibility(self):
        item = normalize_sec_raw_row(sec_row(), {"fiscal_year": 2025, "fiscal_quarter": 3})
        with self.assertRaisesRegex(ValueError, "filed_date"):
            NormalizedObservation(**{**item.__dict__, "filed_date": None})
        with self.assertRaisesRegex(ValueError, "DERIVED"):
            NormalizedObservation(
                **{
                    **item.__dict__,
                    "source": "DERIVED",
                    "source_variant": "derived.q4",
                    "observation_kind": "DERIVED",
                    "filed_date": None,
                }
            )

    def test_original_sec_identity_sets_q4_only_for_explicit_quarterly_10k(self):
        original = sec_row(
            id=1,
            fiscal_quarter=None,
            form_type="10-K",
            period_start=date(2025, 6, 29),
            period_end=date(2025, 9, 27),
        )
        later_amendment = sec_row(id=2, filed_date=date(2026, 1, 30), fiscal_year=2026, fiscal_quarter=1)
        self.assertEqual(
            _original_sec_fiscal_identity([later_amendment, original]),
            {"fiscal_year": 2025, "fiscal_quarter": 4},
        )
        annual = dict(original, period_start=date(2024, 9, 29))
        self.assertIsNone(_original_sec_fiscal_identity([annual])["fiscal_quarter"])

    def test_original_sec_identity_q4_duration_boundaries_and_forms(self):
        period_end = date(2025, 9, 27)
        for duration, form_type, expected in (
            (70, "10-K", 4), (110, "10-K/A", 4), (69, "10-K", None),
            (111, "10-K/A", None), (90, "10-Q", None),
        ):
            with self.subTest(duration=duration, form_type=form_type):
                raw = sec_row(
                    fiscal_quarter=None,
                    form_type=form_type,
                    period_start=period_end - timedelta(days=duration),
                    period_end=period_end,
                )
                self.assertEqual(_original_sec_fiscal_identity([raw])["fiscal_quarter"], expected)

    def test_original_sec_identity_handles_an_iterable_without_fiscal_year(self):
        raw = sec_row(
            id=1,
            fiscal_year=None,
            fiscal_quarter=None,
            form_type="10-K/A",
            period_start=date(2025, 6, 29),
            period_end=date(2025, 9, 27),
        )
        self.assertEqual(_original_sec_fiscal_identity(iter([raw]))["fiscal_quarter"], 4)

    def test_repeated_raw_and_version_return_same_id(self):
        item = normalize_sec_raw_row(sec_row(), {"fiscal_year": 2025, "fiscal_quarter": 3})
        cursor = ScriptedCursor([(101,), None, (101, *item_to_db_row(item))])
        with patch("database.normalized_fundamentals.get_connection", return_value=ScriptedConnection(cursor)):
            self.assertEqual(insert_normalized_batch([item, item]), [101, 101])
        self.assertIn("ON CONFLICT (raw_id, normalizer_version) DO NOTHING", cursor.executed[0][0])
        self.assertIn("WHERE raw_id = %s AND normalizer_version = %s", cursor.executed[2][0])

    def test_same_raw_and_version_with_different_semantics_raises(self):
        item = normalize_sec_raw_row(sec_row(), {"fiscal_year": 2025, "fiscal_quarter": 3})
        for field, replacement in (
            (5, Decimal("9.99")),
            (16, item.observed_at + timedelta(seconds=1)),
            (20, ("MISSING_FISCAL_YEAR",)),
        ):
            with self.subTest(field=field):
                stored = list(item_to_db_row(item))
                stored[field] = replacement
                cursor = ScriptedCursor([None, (101, *stored)])
                with patch("database.normalized_fundamentals.get_connection", return_value=ScriptedConnection(cursor)):
                    with self.assertRaisesRegex(RuntimeError, r"^Conflicto semántico"):
                        insert_normalized_batch([item])

    def test_load_raw_is_ordered_and_optional_source_is_bound(self):
        columns = [
            "id", "company_id", "source", "source_variant", "metric", "period_start", "period_end",
            "filed_date", "fiscal_year", "fiscal_quarter", "form_type", "value", "unit",
            "currency", "xbrl_tag", "source_record_id", "source_payload", "fetched_at", "created_at",
        ]
        cursor = ScriptedCursor([[tuple(sec_row()[column] for column in columns)]])
        cursor.description = [type("Column", (), {"name": column}) for column in columns]
        with patch("database.normalized_fundamentals.get_connection", return_value=ScriptedConnection(cursor)):
            rows = load_raw_fundamentals(1, source="SEC")
        self.assertEqual(rows, [sec_row()])
        sql, params = cursor.executed[0]
        self.assertIn("ORDER BY period_end, metric, filed_date, id", sql)
        self.assertIn("created_at", sql)
        self.assertIn("source_variant", sql)
        self.assertIn("source = %s", sql)
        self.assertEqual(params, (1, "SEC"))

    def test_sec_import_assigns_one_fetched_at_and_source_variant_to_the_raw_batch(self):
        result = {
            "ticker": "AAPL", "cik": "320193", "cik_source": "fixture",
            "selected_tags": {}, "facts": [{"metric": "EPS_DILUTED"}, {"metric": "REVENUE"}],
        }
        with patch("database.sec_import.extract_sec_raw_facts", return_value=result), \
             patch("database.sec_import.insert_raw_fundamentals_batch", return_value=[1, 2]) as insert:
            imported = import_sec_fundamentals("aapl", 1)
        facts = insert.call_args.args[0]
        self.assertEqual(imported["raw_ids"], [1, 2])
        self.assertEqual({fact["source_variant"] for fact in facts}, {"sec.company_facts"})
        self.assertEqual(len({fact["fetched_at"] for fact in facts}), 1)

    def test_raw_persistence_keeps_import_fetched_at_without_a_database_call(self):
        captured = ScriptedCursor([(101,)])
        fetched_at = datetime(2025, 8, 2, 9, 30, tzinfo=timezone.utc)
        fact = {**sec_row(), "fetched_at": fetched_at}
        self.assertEqual(_insert_raw_with_cursor(captured, fact), 101)
        self.assertEqual(captured.executed[0][1][-1], fetched_at)


def item_to_db_row(item):
    return (
        item.company_id,
        item.metric,
        item.source,
        item.source_variant,
        item.observation_kind,
        item.value,
        item.unit,
        item.currency,
        item.source_period_start,
        item.source_period_end,
        item.canonical_period_end,
        item.series_date,
        item.fiscal_year,
        item.fiscal_quarter,
        item.filed_date,
        item.source_available_at,
        item.observed_at,
        item.raw_id,
        item.normalizer_version,
        item.intrinsic_quality_status,
        item.intrinsic_quality_reasons,
        item.selection_eligibility,
        item.alignment_method,
        item.alignment_days,
        item.alignment_reference_id,
    )


if __name__ == "__main__":
    unittest.main()
