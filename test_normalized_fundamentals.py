import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from database.normalized_fundamentals import (
    SEC_NORMALIZER_V2,
    YAHOO_NORMALIZER_V2,
    YAHOO_NORMALIZER_VERSION,
    FiscalIdentityResult,
    NormalizedObservation,
    SEC_NORMALIZER_VERSION,
    _original_sec_fiscal_identity,
    normalize_yahoo_raw_row,
    normalize_yahoo_raw_row_v2,
    normalize_sec_raw_row,
    normalize_sec_raw_row_v2,
    resolve_yahoo_fiscal_identity,
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


def yahoo_row(**overrides):
    row = {
        "id": 409,
        "company_id": 1,
        "source": "YAHOO",
        "source_variant": None,
        "metric": "EPS_DILUTED",
        "period_start": None,
        "period_end": date(2025, 9, 30),
        "filed_date": None,
        "fiscal_year": None,
        "fiscal_quarter": None,
        "form_type": None,
        "value": Decimal("1.85"),
        "unit": "USD/shares",
        "currency": "USD",
        "xbrl_tag": None,
        "source_record_id": "yahoo:abc",
        "source_payload": {"source_available_at": None},
        "fetched_at": datetime(2026, 9, 4, 19, 1, tzinfo=timezone.utc),
        "created_at": datetime(2026, 9, 4, 19, 1, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


def sec_observation(raw_id, fiscal_year, fiscal_quarter, period_end, **overrides):
    overrides.setdefault("period_start", period_end - timedelta(days=90))
    item = normalize_sec_raw_row(
        sec_row(id=raw_id, period_end=period_end, **overrides),
        {"fiscal_year": fiscal_year, "fiscal_quarter": fiscal_quarter},
    )
    return replace(item, id=raw_id + 1000)


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


class YahooNormalizedTests(unittest.TestCase):
    yahoo_variant = "yahoo.fundamentals_timeseries"

    def test_unambiguous_q4_from_fiscal_bookends_keeps_yahoo_date(self):
        calendar = [
            sec_observation(301, 2025, 3, date(2025, 6, 28)),
            sec_observation(302, 2026, 1, date(2025, 12, 27)),
        ]
        item = normalize_yahoo_raw_row(yahoo_row(), self.yahoo_variant, calendar)

        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (2025, 4))
        self.assertEqual(item.canonical_period_end, date(2025, 9, 30))
        self.assertEqual(item.source_period_end, date(2025, 9, 30))
        self.assertEqual(item.series_date, date(2025, 9, 30))
        self.assertEqual(item.alignment_method, "SEC_CALENDAR")
        self.assertEqual(item.selection_eligibility, "ELIGIBLE")

    def test_correlated_calendar_resolves_direct_period_and_records_alignment(self):
        calendar = [
            sec_observation(310, 2025, 2, date(2025, 3, 29)),
            sec_observation(311, 2025, 3, date(2025, 6, 28)),
        ]
        item = normalize_yahoo_raw_row(
            yahoo_row(period_end=date(2025, 6, 30)), self.yahoo_variant, calendar
        )

        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (2025, 3))
        self.assertEqual(item.canonical_period_end, date(2025, 6, 28))
        self.assertEqual(item.alignment_method, "NEAREST_35D")
        self.assertEqual(item.alignment_days, 2)
        self.assertEqual(item.alignment_reference_id, 1311)

    def test_near_corroborated_period_wins_over_a_later_calendar_gap(self):
        calendar = [
            sec_observation(312, 2025, 2, date(2025, 3, 29)),
            sec_observation(313, 2025, 3, date(2025, 6, 28)),
            sec_observation(314, 2026, 1, date(2025, 12, 27)),
        ]
        item = normalize_yahoo_raw_row(
            yahoo_row(period_end=date(2025, 6, 30)), self.yahoo_variant, calendar
        )

        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (2025, 3))
        self.assertEqual(item.alignment_reference_id, 1313)
        self.assertEqual(item.alignment_method, "NEAREST_35D")

    def test_single_nearby_sec_is_not_enough_to_assign_fiscal_identity(self):
        calendar = [sec_observation(320, 2025, 4, date(2025, 9, 19))]
        item = normalize_yahoo_raw_row(
            yahoo_row(period_end=date(2025, 8, 15)), self.yahoo_variant, calendar
        )

        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (None, None))
        self.assertIsNone(item.canonical_period_end)
        self.assertEqual(item.alignment_method, "UNRESOLVED")
        self.assertEqual(item.selection_eligibility, "INELIGIBLE")
        self.assertEqual(item.intrinsic_quality_status, "REVIEW_REQUIRED")
        self.assertIn("PROXIMITY_WITHOUT_FISCAL_IDENTITY_V1", item.intrinsic_quality_reasons)

    def test_fiscal_neighbor_on_the_wrong_side_does_not_corroborate_identity(self):
        calendar = [
            sec_observation(321, 2025, 3, date(2025, 6, 28)),
            sec_observation(322, 2025, 2, date(2025, 9, 27)),
        ]
        item = normalize_yahoo_raw_row(
            yahoo_row(period_end=date(2025, 6, 30)), self.yahoo_variant, calendar
        )

        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (None, None))
        self.assertEqual(item.alignment_method, "UNRESOLVED")

    def test_tied_sec_candidates_remain_ambiguous(self):
        calendar = [
            sec_observation(330, 2025, 3, date(2025, 7, 11)),
            sec_observation(331, 2025, 4, date(2025, 9, 19)),
        ]
        result = resolve_yahoo_fiscal_identity(
            yahoo_row(period_end=date(2025, 8, 15)), calendar
        )

        self.assertIsInstance(result, FiscalIdentityResult)
        self.assertEqual((result.fiscal_year, result.fiscal_quarter), (None, None))
        self.assertIn("AMBIGUOUS_FISCAL_IDENTITY_V1", result.reasons)

    def test_fiscal_contradiction_is_not_resolved_by_proximity(self):
        row = yahoo_row(
            period_end=date(2025, 9, 30), fiscal_year=2025, fiscal_quarter=4
        )
        calendar = [sec_observation(340, 2026, 1, date(2025, 9, 29))]
        item = normalize_yahoo_raw_row(row, self.yahoo_variant, calendar)

        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (None, None))
        self.assertIsNone(item.canonical_period_end)
        self.assertIn("CONTRADICTORY_FISCAL_IDENTITY_V1", item.intrinsic_quality_reasons)

    def test_irregular_calendar_can_infer_the_only_missing_fiscal_quarter(self):
        calendar = [
            sec_observation(350, 2025, 3, date(2025, 6, 7)),
            sec_observation(351, 2026, 1, date(2026, 1, 17)),
        ]
        item = normalize_yahoo_raw_row(
            yahoo_row(period_end=date(2025, 10, 5)), self.yahoo_variant, calendar
        )

        self.assertEqual((item.fiscal_year, item.fiscal_quarter), (2025, 4))
        self.assertEqual(item.alignment_method, "SEC_CALENDAR")

    def test_alignment_limit_is_inclusive_at_35_and_excludes_36(self):
        calendar = [
            sec_observation(360, 2025, 2, date(2025, 3, 29)),
            sec_observation(361, 2025, 3, date(2025, 6, 28)),
        ]
        at_35 = normalize_yahoo_raw_row(
            yahoo_row(
                period_end=date(2025, 8, 2), fiscal_year=2025, fiscal_quarter=3
            ),
            self.yahoo_variant,
            calendar,
        )
        at_36 = normalize_yahoo_raw_row(
            yahoo_row(
                id=410,
                period_end=date(2025, 8, 3),
                fiscal_year=2025,
                fiscal_quarter=3,
            ),
            self.yahoo_variant,
            calendar,
        )

        self.assertEqual((at_35.alignment_method, at_35.alignment_days), ("NEAREST_35D", 35))
        self.assertEqual(at_35.alignment_reference_id, 1361)
        self.assertEqual((at_36.alignment_method, at_36.alignment_days), ("FISCAL_METADATA", 0))
        self.assertIsNone(at_36.alignment_reference_id)

    def test_yahoo_contract_preserves_variant_dates_availability_and_reported_kind(self):
        calendar = [
            sec_observation(370, 2025, 3, date(2025, 6, 28)),
            sec_observation(371, 2026, 1, date(2025, 12, 27)),
        ]
        for variant in (
            "yahoo.fundamentals_timeseries",
            "yfinance.quarterly_income_stmt",
        ):
            with self.subTest(variant=variant):
                item = normalize_yahoo_raw_row(yahoo_row(), variant, calendar)
                self.assertEqual(item.source_variant, variant)
                self.assertEqual(item.source_period_end, date(2025, 9, 30))
                self.assertEqual(item.series_date, date(2025, 9, 30))
                self.assertIsNone(item.source_available_at)
                self.assertEqual(item.observation_kind, "REPORTED")
                self.assertNotEqual(item.source, "DERIVED")
                self.assertEqual(item.normalizer_version, YAHOO_NORMALIZER_VERSION)

    def test_repeated_yahoo_raw_and_version_return_same_normalized_id(self):
        calendar = [
            sec_observation(380, 2025, 3, date(2025, 6, 28)),
            sec_observation(381, 2026, 1, date(2025, 12, 27)),
        ]
        item = normalize_yahoo_raw_row(yahoo_row(), self.yahoo_variant, calendar)
        cursor = ScriptedCursor([(201,), None, (201, *item_to_db_row(item))])
        with patch(
            "database.normalized_fundamentals.get_connection",
            return_value=ScriptedConnection(cursor),
        ):
            self.assertEqual(insert_normalized_batch([item, item]), [201, 201])


class SemanticsV2Tests(unittest.TestCase):
    def test_sec_v2_preserves_xbrl_tag_unit_and_scale(self):
        item = normalize_sec_raw_row_v2(
            sec_row(), {"fiscal_year": 2025, "fiscal_quarter": 3}
        )
        self.assertEqual(item.normalizer_version, SEC_NORMALIZER_V2)
        self.assertEqual(item.metric, "EPS_DILUTED")
        self.assertEqual(item.source_metric_name, "EarningsPerShareDiluted")
        self.assertEqual(item.source_unit, "USD/shares")
        self.assertEqual(item.source_scale_factor, Decimal("1"))

    def test_yahoo_timeseries_explicitly_proves_diluted_eps(self):
        row = yahoo_row(source_payload={
            "source_variant": "yahoo.fundamentals_timeseries",
            "provider_id": "quarterlyDilutedEPS:2025-09-30",
        })
        item = normalize_yahoo_raw_row_v2(
            row,
            "yahoo.fundamentals_timeseries",
            [
                sec_observation(301, 2025, 3, date(2025, 6, 28)),
                sec_observation(302, 2026, 1, date(2025, 12, 27)),
            ],
        )
        self.assertEqual(item.normalizer_version, YAHOO_NORMALIZER_V2)
        self.assertEqual(item.metric, "EPS_DILUTED")
        self.assertEqual(item.source_metric_name, "quarterlyDilutedEPS")
        self.assertEqual(item.source_unit, "USD/shares")
        self.assertEqual(item.source_scale_factor, Decimal("1"))

    def test_future_yfinance_eps_preserves_exact_basic_or_diluted_alias(self):
        cases = (
            ("Basic EPS", "EPS_BASIC"), ("BasicEPS", "EPS_BASIC"),
            ("Diluted EPS", "EPS_DILUTED"), ("DilutedEPS", "EPS_DILUTED"),
        )
        calendar = [
            sec_observation(301, 2025, 3, date(2025, 6, 28)),
            sec_observation(302, 2026, 1, date(2025, 12, 27)),
        ]
        for alias, metric in cases:
            with self.subTest(alias=alias):
                row = yahoo_row(
                    metric=metric,
                    source_payload={
                        "source_variant": "yfinance.quarterly_income_stmt",
                        "source_metric_name": alias,
                        "source_unit": "USD/shares",
                        "source_scale_factor": "1",
                    },
                )
                item = normalize_yahoo_raw_row_v2(
                    row, "yfinance.quarterly_income_stmt", calendar
                )
                self.assertEqual(item.metric, metric)
                self.assertEqual(item.source_metric_name, alias)

    def test_historical_yfinance_eps_without_alias_remains_unspecified(self):
        row = yahoo_row(source_payload={
            "source_variant": "yfinance.quarterly_income_stmt",
            "provider_id": "EPS_DILUTED:2025-09-30",
        })
        item = normalize_yahoo_raw_row_v2(
            row,
            "yfinance.quarterly_income_stmt",
            [
                sec_observation(301, 2025, 3, date(2025, 6, 28)),
                sec_observation(302, 2026, 1, date(2025, 12, 27)),
            ],
        )
        self.assertEqual(item.metric, "EPS_UNSPECIFIED")
        self.assertEqual(item.source_metric_name, "legacy.unknown")
        self.assertEqual(item.intrinsic_quality_status, "REVIEW_REQUIRED")
        self.assertEqual(item.selection_eligibility, "INELIGIBLE")

    def test_eps_unspecified_can_never_be_eligible(self):
        row = yahoo_row(source_payload={
            "source_variant": "yfinance.quarterly_income_stmt",
            "provider_id": "EPS_DILUTED:2025-09-30",
        })
        item = normalize_yahoo_raw_row_v2(
            row,
            "yfinance.quarterly_income_stmt",
            [
                sec_observation(301, 2025, 3, date(2025, 6, 28)),
                sec_observation(302, 2026, 1, date(2025, 12, 27)),
            ],
        )
        with self.assertRaisesRegex(ValueError, "inelegible|EPS_UNSPECIFIED"):
            replace(item, selection_eligibility="ELIGIBLE")


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
        item.source_metric_name,
        item.source_unit,
        item.source_scale_factor,
    )


if __name__ == "__main__":
    unittest.main()
