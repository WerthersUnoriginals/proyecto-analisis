"""Pure contracts plus opt-in, rollback-only PostgreSQL projection equivalence."""

import os
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timedelta, timezone
from itertools import permutations
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import database.effective_fundamentals as effective_module
from database.normalized_fundamentals import NormalizedObservation
from database.effective_fundamentals import (
    AnnualComparison,
    ComparisonDiagnostic,
    EffectiveObservation,
    YoYGrowth,
    annual_comparisons_by_source,
    compare_observations,
    growth_acceleration_by_source,
    growth_yoy_by_source,
    select_effective_observations,
)


class LoaderCursor:
    def __init__(self, columns, rows):
        self.description = [SimpleNamespace(name=name) for name in columns]
        self.rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params):
        self.calls.append((query, params))

    def fetchall(self):
        return self.rows


class LoaderConnection:
    def __init__(self, columns, rows):
        self.loader_cursor = LoaderCursor(columns, rows)
        self.closed = False

    def cursor(self):
        return self.loader_cursor

    def close(self):
        self.closed = True

    def commit(self):
        raise AssertionError("A read-only loader must not commit")


class EffectiveCurrentLoaderTests(unittest.TestCase):
    def run_loader(self, company_id, columns, rows):
        connection = LoaderConnection(columns, rows)
        with patch.object(effective_module, "get_connection", return_value=connection):
            result = effective_module.load_effective_current(company_id)
        return result, connection

    def test_valid_company_returns_dicts_with_native_values_and_none(self):
        columns = ("selected_observation_id", "value", "comparison_reason")
        rows = [(101, Decimal("1.25"), None)]

        result, connection = self.run_loader(7, columns, rows)

        self.assertEqual(result, [{
            "selected_observation_id": 101,
            "value": Decimal("1.25"),
            "comparison_reason": None,
        }])
        self.assertTrue(connection.closed)

    def test_company_without_rows_returns_empty_list(self):
        result, _ = self.run_loader(999, ("selected_observation_id",), [])

        self.assertEqual(result, [])

    def test_query_is_parameterized_read_only_and_uses_only_the_view(self):
        _, connection = self.run_loader(17, ("selected_observation_id",), [])

        self.assertEqual(len(connection.loader_cursor.calls), 1)
        query, params = connection.loader_cursor.calls[0]
        normalized_query = " ".join(query.lower().split())
        self.assertEqual(params, (17,))
        self.assertIn("where company_id = %s", normalized_query)
        self.assertNotIn("17", query)
        self.assertIn("from fundamentals_effective_current", normalized_query)
        self.assertNotIn("fundamentals_normalized", normalized_query)
        self.assertNotIn("fundamentals_raw", normalized_query)
        self.assertIn(
            "order by company_id, metric, series_date, source_variant, raw_id",
            normalized_query,
        )
        for forbidden in ("insert", "update", "delete", "truncate", "alter", "drop", "create"):
            self.assertNotIn(forbidden, normalized_query.split())


def observation(source, value="100", **changes):
    sec = source == "SEC"
    fields = dict(
        id=101 if sec else 202, company_id=1, metric="EPS_DILUTED",
        source=source,
        source_variant="sec.company_facts" if sec else "yahoo.fundamentals_timeseries",
        observation_kind="REPORTED", value=Decimal(value), unit="USD/shares",
        currency="USD", source_period_start=date(2025, 3, 30) if sec else None,
        source_period_end=date(2025, 6, 28), canonical_period_end=date(2025, 6, 28),
        series_date=date(2025, 6, 28), fiscal_year=2025, fiscal_quarter=3,
        filed_date=date(2025, 8, 1) if sec else None, source_available_at=None,
        observed_at=datetime(2026, 9, 4, tzinfo=timezone.utc), raw_id=1 if sec else 2,
        normalizer_version="sec-normalized-v2" if sec else "yahoo-normalized-v2",
        intrinsic_quality_status="OK", intrinsic_quality_reasons=(),
        selection_eligibility="ELIGIBLE", alignment_method="EXACT", alignment_days=0,
        alignment_reference_id=None,
        source_metric_name="EarningsPerShareDiluted" if sec else "quarterlyDilutedEPS",
        source_unit="USD/shares", source_scale_factor=Decimal("1"),
    )
    fields.update(changes)
    return NormalizedObservation(**fields)


class ComparisonTests(unittest.TestCase):
    def assert_incompatible(self, sec, yahoo, reason):
        result = compare_observations(sec, yahoo)
        self.assertIsNone(result.comparison_diff_pct)
        self.assertIn(reason, result.comparison_reasons)
        self.assertEqual(result.comparison_status, "REVIEW_REQUIRED")
        self.assertEqual(result.comparison_rules_version, "sec-yahoo-comparison-v1")
        return result

    def test_exact_percentage_boundaries_through_real_comparator(self):
        # Denominator is exactly 100; input differences are percentages already.
        for yahoo_value, pct, status in (
            ("100", 0.0, "MINOR_DIFFERENCE"),
            ("99.9", 0.1, "MINOR_DIFFERENCE"),
            ("99.899999", 0.100001, "DISCREPANCY_RECORDED"),
            ("99.000001", 0.999999, "DISCREPANCY_RECORDED"),
            ("99", 1.0, "REVIEW_REQUIRED"),
            ("95", 5.0, "REVIEW_REQUIRED"),
            ("94.999999", 5.000001, "REVIEW_REQUIRED_HIGH"),
        ):
            with self.subTest(yahoo_value=yahoo_value):
                result = compare_observations(observation("SEC"), observation("YAHOO", yahoo_value))
                self.assertEqual(result.comparison_diff_pct, pct)
                self.assertEqual(result.comparison_status, status)

    def test_both_zero(self):
        result = compare_observations(observation("SEC", "0"), observation("YAHOO", "0"))
        self.assertEqual(result.comparison_diff_pct, 0.0)
        self.assertEqual(result.comparison_status, "MINOR_DIFFERENCE")

    def test_one_zero_uses_contractual_denominator(self):
        result = compare_observations(observation("SEC", "0"), observation("YAHOO", "1"))
        self.assertEqual(result.comparison_diff_pct, 100.0)
        self.assertEqual(result.comparison_status, "REVIEW_REQUIRED_HIGH")

    def test_denominator_is_maximum_absolute_value_and_symmetric(self):
        for first, second in (("100", "125"), ("125", "100"), ("-100", "-125")):
            with self.subTest(first=first):
                result = compare_observations(observation("SEC", first), observation("YAHOO", second))
                self.assertEqual(result.comparison_diff_pct, 20.0)

    def test_basic_compares_only_to_proven_basic(self):
        sec = observation("SEC", metric="EPS_BASIC", source_metric_name="EarningsPerShareBasic")
        yahoo = observation("YAHOO", metric="EPS_BASIC", source_variant="yfinance.quarterly_income_stmt", source_metric_name="Basic EPS")
        self.assertEqual(compare_observations(sec, yahoo).comparison_diff_pct, 0.0)
        self.assert_incompatible(sec, observation("YAHOO"), "METRIC_MISMATCH")
        self.assert_incompatible(observation("SEC"), yahoo, "METRIC_MISMATCH")

    def test_unspecified_never_compares_and_legacy_unknown_is_not_inferred(self):
        yahoo = observation("YAHOO", metric="EPS_UNSPECIFIED", source_variant="yfinance.quarterly_income_stmt", source_metric_name="legacy.unknown", intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        for metric, tag in (("EPS_DILUTED", "EarningsPerShareDiluted"), ("EPS_BASIC", "EarningsPerShareBasic")):
            self.assert_incompatible(observation("SEC", metric=metric, source_metric_name=tag), yahoo, "EPS_SEMANTICS_UNSPECIFIED")

    def test_eps_alias_contradictions_and_unknown_tags_are_rejected(self):
        for alias in ("Basic EPS", "legacy.unknown", "unprovenDilutedName"):
            with self.subTest(alias=alias):
                self.assert_incompatible(observation("SEC"), observation("YAHOO", source_metric_name=alias), "YAHOO_EPS_SEMANTICS_UNPROVEN")

    def test_sec_tag_must_prove_eps_semantics(self):
        self.assert_incompatible(observation("SEC", source_metric_name="EarningsPerShareBasic"), observation("YAHOO"), "SEC_EPS_SEMANTICS_UNPROVEN")

    def test_dataset_cannot_borrow_another_provider_alias(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_variant="yfinance.quarterly_income_stmt"), "YAHOO_EPS_SEMANTICS_UNPROVEN")

    def test_v1_and_mixed_versions_are_rejected(self):
        for sv, yv in (("sec-normalized-v1", "yahoo-normalized-v2"), ("sec-normalized-v2", "yahoo-normalized-v1"), ("sec-normalized-v1", "yahoo-normalized-v1")):
            self.assert_incompatible(observation("SEC", normalizer_version=sv), observation("YAHOO", normalizer_version=yv), "NORMALIZER_VERSION_MISMATCH")

    def test_roles_and_company_must_match(self):
        self.assert_incompatible(observation("YAHOO"), observation("SEC"), "SOURCE_ROLE_MISMATCH")
        self.assert_incompatible(observation("SEC"), observation("YAHOO", company_id=2), "COMPANY_MISMATCH")

    def test_metric_and_normalized_unit_mismatch(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", metric="REVENUE", unit="USD", source_unit="USD", source_metric_name="quarterlyTotalRevenue"), "UNIT_MISMATCH")

    def test_source_units_must_agree_with_normalized_representation(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_unit="cents/shares"), "SOURCE_UNIT_INCOMPATIBLE")

    def test_scale_mismatch_is_not_silently_converted(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_scale_factor=Decimal("1000")), "SCALE_MISMATCH")

    def test_equal_documented_factors_need_no_conversion(self):
        sec = observation("SEC", source_scale_factor=Decimal("1000"))
        yahoo = observation("YAHOO", source_scale_factor=Decimal("1000"))
        self.assertEqual(compare_observations(sec, yahoo).comparison_diff_pct, 0.0)

    def test_infinite_scale_is_rejected_even_when_dataclass_accepts_it(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_scale_factor=Decimal("Infinity")), "INVALID_SCALE")

    def test_conflicting_explicit_period_starts_are_not_compared(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", source_period_start=date(2025, 1, 1)), "PERIOD_START_INCOMPATIBLE")

    def test_currency_mismatch_or_missing_monetary_currency(self):
        for currency in ("EUR", None):
            self.assert_incompatible(observation("SEC"), observation("YAHOO", currency=currency), "CURRENCY_INCOMPATIBLE")

    def test_fiscal_identity_must_exist_and_match(self):
        for fields in ({"fiscal_year": 2026}, {"fiscal_quarter": 4}, {"fiscal_year": None}, {"fiscal_quarter": None}):
            self.assert_incompatible(observation("SEC"), observation("YAHOO", **fields), "FISCAL_IDENTITY_INCOMPATIBLE")

    def test_canonical_period_mismatch_is_not_fixed_by_proximity(self):
        self.assert_incompatible(observation("SEC"), observation("YAHOO", canonical_period_end=date(2025, 6, 27), alignment_days=1, alignment_method="NEAREST_35D"), "CANONICAL_PERIOD_INCOMPATIBLE")

    def test_resolved_alignment_preserves_distinct_source_dates(self):
        yahoo = observation("YAHOO", source_period_end=date(2025, 6, 30), series_date=date(2025, 6, 30), alignment_method="NEAREST_35D", alignment_days=2, alignment_reference_id=101)
        self.assertEqual(compare_observations(observation("SEC"), yahoo).comparison_diff_pct, 0.0)

    def test_unresolved_identity_is_not_numerically_compared(self):
        yahoo = observation("YAHOO", canonical_period_end=None, fiscal_year=None, fiscal_quarter=None, alignment_days=None, alignment_method="UNRESOLVED", intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        self.assert_incompatible(observation("SEC"), yahoo, "CANONICAL_PERIOD_INCOMPATIBLE")

    def test_material_sign_conflict(self):
        self.assert_incompatible(observation("SEC", "1"), observation("YAHOO", "-1"), "SIGN_INCOMPATIBLE")

    def test_nonfinite_values_do_not_enter_arithmetic(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            self.assert_incompatible(observation("SEC", value), observation("YAHOO"), "NONFINITE_VALUE")

    def test_diagnostic_is_frozen_and_references_sec_counterpart(self):
        result = compare_observations(observation("SEC"), observation("YAHOO"))
        self.assertEqual(result.comparison_reference_id, 101)
        self.assertEqual(result.comparison_rules_version, "sec-yahoo-comparison-v1")
        with self.assertRaises(FrozenInstanceError):
            result.comparison_status = "NOT_COMPARED"
        self.assertIsNone(compare_observations(observation("SEC", id=None), observation("YAHOO")).comparison_reference_id)

    def test_reasons_are_deterministic_and_inputs_unchanged_for_all_paths(self):
        sec = observation("SEC")
        for yahoo in (observation("YAHOO"), observation("YAHOO", "90"), observation("YAHOO", currency="EUR", fiscal_year=2026, source_unit="cents/shares")):
            before = deepcopy((asdict(sec), asdict(yahoo)))
            first = compare_observations(sec, yahoo)
            self.assertEqual(first, compare_observations(sec, yahoo))
            self.assertEqual((asdict(sec), asdict(yahoo)), before)
            self.assertIsInstance(first.comparison_reasons, tuple)


class SelectionTests(unittest.TestCase):
    def yfinance(self, offset=0, **fields):
        end = date(2025, 6, 28) + timedelta(days=offset)
        fields = dict(dict(id=203, raw_id=3,
            source_variant="yfinance.quarterly_income_stmt",
            source_metric_name="Diluted EPS", source_period_end=end, series_date=end,
            alignment_method="FISCAL_METADATA", alignment_days=offset), **fields)
        return observation("YAHOO", **fields)

    def test_yahoo_priority_same_date_and_inclusive_window_in_both_orders(self):
        ts = observation("YAHOO")
        for offset in (0, -35, 35, -36, 36):
            yf = self.yfinance(offset)
            expected = {202} if abs(offset) <= 35 else {202, 203}
            for order in ((ts, yf), (yf, ts)):
                with self.subTest(offset=offset, order=order[0].id):
                    result = select_effective_observations(order)
                    self.assertEqual({x.observation.id for x in result}, expected)

    def test_sec_priority_same_date_and_inclusive_window_in_both_orders(self):
        sec = observation("SEC")
        for offset in (0, -35, 35, -36, 36):
            yahoo = self.yfinance(offset)
            for order in ((sec, yahoo), (yahoo, sec)):
                with self.subTest(offset=offset):
                    result = select_effective_observations(order)
                    expected = {101} if abs(offset) <= 35 else {101, 203}
                    self.assertEqual({x.observation.id for x in result}, expected)

    def test_yahoo_is_resolved_before_sec_comparison(self):
        sec, ts, yf = observation("SEC"), observation("YAHOO", "95"), self.yfinance(value="90")
        result = select_effective_observations([yf, ts, sec])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].comparison, compare_observations(sec, ts))

    def test_review_and_high_numeric_review_preserve_sec_priority(self):
        for value, status in (("95", "REVIEW_REQUIRED"), ("90", "REVIEW_REQUIRED_HIGH")):
            sec, yahoo = observation("SEC"), observation("YAHOO", value)
            result = select_effective_observations([yahoo, sec])
            self.assertEqual([x.observation for x in result], [sec])
            self.assertEqual(result[0].comparison.comparison_status, status)
            self.assertEqual(result[0].comparison, compare_observations(sec, yahoo))

    def test_fallback_has_explicit_reason_and_no_fabricated_comparison(self):
        yahoo = observation("YAHOO")
        result = select_effective_observations([yahoo])[0]
        self.assertIs(result.observation, yahoo)
        self.assertEqual(result.selection_reason, "YAHOO_FALLBACK_NO_SEC_WITHIN_35D")
        self.assertEqual(result.selection_policy_version, "c-v2.6-compatible-v1")
        self.assertEqual(result.comparison.comparison_status, "NOT_COMPARED")
        self.assertIsNone(result.comparison.comparison_diff_pct)
        self.assertIsNone(result.comparison.comparison_reference_id)

    def test_v1_ineligible_and_unknown_source_variants_are_excluded(self):
        rows = [observation("SEC", normalizer_version="sec-normalized-v1"),
            observation("YAHOO", normalizer_version="yahoo-normalized-v1"),
            observation("SEC", selection_eligibility="INELIGIBLE"),
            observation("YAHOO", source_variant="yahoo.future")]
        self.assertEqual(select_effective_observations(rows), [])

    def test_derived_is_excluded(self):
        row = replace(observation("YAHOO"), source="DERIVED", source_variant="derived.future",
            observation_kind="DERIVED", selection_eligibility="INELIGIBLE")
        self.assertEqual(select_effective_observations([row]), [])

    def test_basic_and_unspecified_cannot_supply_c_diluted(self):
        basic = self.yfinance(metric="EPS_BASIC", source_metric_name="Basic EPS")
        unknown = self.yfinance(metric="EPS_UNSPECIFIED", source_metric_name="legacy.unknown",
            intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        self.assertEqual(select_effective_observations([basic, unknown]), [])
        diluted = observation("YAHOO")
        self.assertEqual([x.observation for x in select_effective_observations([basic, diluted])], [diluted])

    def test_yahoo_shares_excluded_while_sec_shares_remain(self):
        fields = dict(metric="DILUTED_SHARES", unit="shares", source_unit="shares", currency=None,
            source_metric_name="WeightedAverageNumberOfDilutedSharesOutstanding")
        sec, yahoo = observation("SEC", **fields), observation("YAHOO", **fields)
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo])], [sec])

    def test_unresolved_and_missing_fiscal_identity_are_excluded(self):
        unresolved = observation("YAHOO", fiscal_year=None, fiscal_quarter=None,
            canonical_period_end=None, alignment_method="UNRESOLVED", alignment_days=None,
            intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE")
        missing = observation("YAHOO", fiscal_year=None)
        self.assertEqual(select_effective_observations([unresolved, missing]), [])

    def test_sec_latest_filing_then_raw_id_uses_contractual_ranking(self):
        first = observation("SEC")
        later = replace(first, id=102, raw_id=4, filed_date=date(2025, 8, 2), value=Decimal("101"))
        tie = replace(later, id=103, raw_id=5, value=Decimal("102"))
        for order in permutations([first, later, tie]):
            self.assertEqual([x.observation for x in select_effective_observations(order)], [tie])

    def test_material_yahoo_snapshot_tie_is_excluded_without_latest_heuristic(self):
        first = observation("YAHOO")
        later = replace(first, id=204, raw_id=4, value=Decimal("101"),
            observed_at=first.observed_at + timedelta(days=1))
        for order in ((first, later), (later, first)):
            self.assertEqual(select_effective_observations(order), [])


def sql_fixture_observation(source, row_id, company_id, end, value="100", metric="EPS_DILUTED", **changes):
    unit = {
        "EPS_DILUTED": "USD/shares",
        "EPS_BASIC": "USD/shares",
        "EPS_UNSPECIFIED": "USD/shares",
        "REVENUE": "USD",
        "NET_INCOME": "USD",
        "DILUTED_SHARES": "shares",
    }[metric]
    source_metric_name = {
        ("SEC", "EPS_DILUTED"): "EarningsPerShareDiluted",
        ("SEC", "EPS_BASIC"): "EarningsPerShareBasic",
        ("SEC", "REVENUE"): "RevenueFromContractWithCustomerExcludingAssessedTax",
        ("SEC", "NET_INCOME"): "NetIncomeLoss",
        ("SEC", "DILUTED_SHARES"): "WeightedAverageNumberOfDilutedSharesOutstanding",
        ("YAHOO", "EPS_DILUTED"): "quarterlyDilutedEPS",
        ("YAHOO", "EPS_BASIC"): "Basic EPS",
        ("YAHOO", "EPS_UNSPECIFIED"): "legacy.unknown",
        ("YAHOO", "REVENUE"): "quarterlyTotalRevenue",
        ("YAHOO", "NET_INCOME"): "quarterlyNetIncome",
    }.get((source, metric), "derived.test")
    canonical = changes.pop("canonical_period_end", end)
    alignment_days = None if canonical is None else (end - canonical).days
    alignment_method = "EXACT" if alignment_days == 0 else "NEAREST_35D"
    fields = dict(
        id=row_id,
        raw_id=row_id,
        company_id=company_id,
        metric=metric,
        source_period_start=end - timedelta(days=90) if source == "SEC" else None,
        source_period_end=end,
        canonical_period_end=canonical,
        series_date=end,
        fiscal_year=2025,
        fiscal_quarter=3,
        value=value,
        unit=unit,
        source_unit=unit,
        currency=None if unit == "shares" else "USD",
        source_metric_name=source_metric_name,
        alignment_method=alignment_method,
        alignment_days=alignment_days,
        alignment_reference_id=None,
        filed_date=date(2025, 8, 1) if source == "SEC" else None,
    )
    fields.update(changes)
    return observation(source, **fields)


def sql_equivalence_fixture():
    rows = [
        # Inclusive 35-day SEC/Yahoo comparison; TS suppresses yfinance.
        sql_fixture_observation("SEC", 1001, 10, date(2025, 6, 28)),
        sql_fixture_observation("YAHOO", 1002, 10, date(2025, 8, 2), canonical_period_end=date(2025, 6, 28)),
        sql_fixture_observation(
            "YAHOO", 1003, 10, date(2025, 8, 2), canonical_period_end=date(2025, 6, 28),
            source_variant="yfinance.quarterly_income_stmt", source_metric_name="Diluted EPS",
        ),
        # 36 days is outside the window: both SEC and Yahoo fallback remain.
        sql_fixture_observation("SEC", 1101, 11, date(2025, 6, 28), metric="REVENUE"),
        sql_fixture_observation(
            "YAHOO", 1102, 11, date(2025, 8, 3), metric="REVENUE",
            canonical_period_end=date(2025, 6, 28),
        ),
        # SEC without Yahoo.
        sql_fixture_observation("SEC", 1201, 12, date(2025, 6, 28), metric="NET_INCOME"),
        # Append-only SEC filings: later filing/raw rank wins.
        sql_fixture_observation(
            "SEC", 1301, 13, date(2025, 6, 28), value="90", metric="REVENUE",
            filed_date=date(2025, 7, 1), raw_id=1301,
        ),
        sql_fixture_observation(
            "SEC", 1302, 13, date(2025, 6, 28), value="100", metric="REVENUE",
            filed_date=date(2025, 8, 1), raw_id=1302,
        ),
        # Equidistant compatible SEC candidates create a material tie.
        sql_fixture_observation(
            "SEC", 1401, 14, date(2025, 5, 24), canonical_period_end=date(2025, 6, 28),
        ),
        sql_fixture_observation(
            "SEC", 1402, 14, date(2025, 8, 2), canonical_period_end=date(2025, 6, 28),
        ),
        sql_fixture_observation("YAHOO", 1403, 14, date(2025, 6, 28)),
        # Sign and structural conflicts must not create Yahoo fallback.
        sql_fixture_observation("SEC", 1501, 15, date(2025, 6, 28), value="1"),
        sql_fixture_observation("YAHOO", 1502, 15, date(2025, 6, 28), value="-1"),
        sql_fixture_observation("SEC", 1601, 16, date(2025, 6, 28), metric="REVENUE"),
        sql_fixture_observation(
            "YAHOO", 1602, 16, date(2025, 6, 28), metric="REVENUE", currency="EUR",
        ),
        # Explicit exclusions.
        sql_fixture_observation("SEC", 1701, 17, date(2025, 6, 28), metric="EPS_BASIC"),
        sql_fixture_observation(
            "YAHOO", 1702, 17, date(2025, 6, 28), metric="EPS_UNSPECIFIED",
            source_variant="yfinance.quarterly_income_stmt", source_metric_name="legacy.unknown",
            intrinsic_quality_status="REVIEW_REQUIRED", selection_eligibility="INELIGIBLE",
        ),
        replace(
            sql_fixture_observation(
                "YAHOO", 1703, 17, date(2025, 6, 28), metric="REVENUE",
            ),
            source="DERIVED", source_variant="derived.test", observation_kind="DERIVED",
            normalizer_version="derived-v1", selection_eligibility="INELIGIBLE",
        ),
        sql_fixture_observation(
            "SEC", 1704, 17, date(2025, 6, 28), normalizer_version="sec-normalized-v1",
        ),
    ]
    return rows


@unittest.skipUnless(
    os.getenv("CANSLIM_TEST_POSTGRES") == "1",
    "set CANSLIM_TEST_POSTGRES=1 for rollback-only PostgreSQL equivalence",
)
class SqlProjectionEquivalenceTests(unittest.TestCase):
    migration_path = (
        Path(__file__).resolve().parent
        / "database"
        / "migrations"
        / "2026-09-20_fundamentals_effective_current_v2.sql"
    )

    def execute_projection(self, observations):
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        from database.db import get_connection

        columns = tuple(NormalizedObservation.__dataclass_fields__)
        placeholders = ", ".join(["%s"] * len(columns))
        insert_sql = (
            f"INSERT INTO fundamentals_normalized ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        connection = get_connection()
        try:
            connection.execute(
                "CREATE TEMP TABLE fundamentals_normalized "
                "(LIKE public.fundamentals_normalized INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            values = []
            for item in observations:
                row = asdict(item)
                row["intrinsic_quality_reasons"] = Jsonb(list(item.intrinsic_quality_reasons))
                values.append(tuple(row[column] for column in columns))
            with connection.cursor() as cursor:
                cursor.executemany(insert_sql, values)
            ddl = self.migration_path.read_text(encoding="utf-8").replace(
                "CREATE VIEW fundamentals_effective_current",
                "CREATE TEMP VIEW fundamentals_effective_current",
                1,
            )
            connection.execute(ddl)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SELECT * FROM fundamentals_effective_current")
                return cursor.fetchall()
        finally:
            connection.rollback()
            connection.close()

    def assert_projection_matches_python(self, observations):
        expected = select_effective_observations(observations)
        actual = self.execute_projection(observations)
        self.assertEqual(len(actual), len(expected))
        core_fields = (
            "selected_observation_id",
            "metric",
            "series_date",
            "value",
            "source",
            "source_variant",
            "selection_reason",
            "comparison_status",
            "comparison_reason",
            "comparison_reference_id",
        )
        expected_rows = []
        for item in expected:
            observation_row = item.observation
            expected_rows.append({
                "selected_observation_id": observation_row.id,
                "metric": observation_row.metric,
                "series_date": observation_row.series_date,
                "value": observation_row.value,
                "source": observation_row.source,
                "source_variant": observation_row.source_variant,
                "selection_reason": item.selection_reason,
                "comparison_status": item.comparison.comparison_status,
                "comparison_reason": (
                    item.comparison.comparison_reasons[0]
                    if item.comparison.comparison_reasons else None
                ),
                "comparison_reference_id": item.comparison.comparison_reference_id,
                "comparison_difference_pct": item.comparison.comparison_diff_pct,
            })
        self.assertEqual(
            [[row[field] for field in core_fields] for row in actual],
            [[row[field] for field in core_fields] for row in expected_rows],
        )
        for sql_row, python_row in zip(actual, expected_rows):
            expected_difference = python_row["comparison_difference_pct"]
            if expected_difference is None:
                self.assertIsNone(sql_row["comparison_difference_pct"])
            else:
                self.assertAlmostEqual(
                    float(sql_row["comparison_difference_pct"]), expected_difference, places=12,
                )
        return actual

    def test_sql_matches_python_for_all_task_9a_synthetic_cases(self):
        fixture = sql_equivalence_fixture()
        forward = self.assert_projection_matches_python(fixture)
        reverse = self.assert_projection_matches_python(list(reversed(fixture)))
        self.assertEqual(forward, reverse)

        selected_ids = {row["selected_observation_id"] for row in forward}
        self.assertIn(1001, selected_ids)  # inclusive 35-day SEC priority
        self.assertNotIn(1002, selected_ids)
        self.assertNotIn(1003, selected_ids)  # TS suppresses yfinance
        self.assertIn(1102, selected_ids)  # 36-day Yahoo fallback
        self.assertNotIn(1301, selected_ids)
        self.assertIn(1302, selected_ids)  # later SEC amendment
        self.assertNotIn(1403, selected_ids)  # material nearest tie
        self.assertNotIn(1502, selected_ids)  # sign conflict
        self.assertNotIn(1602, selected_ids)  # structural conflict
        for excluded in (1701, 1702, 1703, 1704):
            self.assertNotIn(excluded, selected_ids)


class SelectionResolutionTests(unittest.TestCase):
    def yfinance(self, offset=0, **fields):
        end = date(2025, 6, 28) + timedelta(days=offset)
        fields = dict(dict(
            id=203,
            raw_id=3,
            source_variant="yfinance.quarterly_income_stmt",
            source_metric_name="Diluted EPS",
            source_period_end=end,
            series_date=end,
            alignment_method="FISCAL_METADATA",
            alignment_days=offset,
        ), **fields)
        return observation("YAHOO", **fields)

    def test_same_sec_rank_with_conflicting_content_is_excluded(self):
        first = observation("SEC")
        conflict = replace(first, id=105, value=Decimal("101"))
        self.assertEqual(select_effective_observations([first, conflict]), [])

    def test_contradictory_fiscal_identity_in_a_source_period_is_excluded(self):
        first = observation("SEC")
        conflict = replace(first, id=105, raw_id=5, fiscal_year=2026)
        self.assertEqual(select_effective_observations([first, conflict]), [])

    def test_equidistant_sec_counterparts_do_not_invent_yahoo_association(self):
        first = observation("SEC", source_period_start=None)
        second = replace(first, id=102, raw_id=2, source_period_end=date(2025, 6, 30),
            series_date=date(2025, 6, 30), alignment_method="FISCAL_METADATA", alignment_days=2)
        yahoo = self.yfinance(1)
        result = select_effective_observations([first, second, yahoo])
        self.assertEqual({x.observation.id for x in result}, {101, 102})
        self.assertTrue(all(x.comparison.comparison_status == "NOT_COMPARED" for x in result))

    def test_series_date_and_all_input_fields_are_preserved(self):
        yahoo = self.yfinance(36)
        sec = observation("SEC")
        before = deepcopy([asdict(sec), asdict(yahoo)])
        result = select_effective_observations([yahoo, sec])
        self.assertEqual([asdict(sec), asdict(yahoo)], before)
        self.assertEqual(next(x.observation.series_date for x in result if x.observation.source == "YAHOO"), date(2025, 8, 3))
        with self.assertRaises(FrozenInstanceError):
            result[0].selection_reason = "changed"

    def test_as_of_filters_before_ranking_and_includes_exact_boundary(self):
        sec = observation("SEC")
        yahoo = replace(observation("YAHOO"), observed_at=sec.observed_at - timedelta(days=1))
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo], yahoo.observed_at)], [yahoo])
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo], sec.observed_at)], [sec])

    def test_result_order_is_stable_for_all_input_permutations(self):
        rows = [observation("SEC"), observation("YAHOO"), self.yfinance(36)]
        expected = select_effective_observations(rows)
        for order in permutations(rows):
            self.assertEqual(select_effective_observations(order), expected)

    def test_ineligible_sec_allows_yahoo_fallback(self):
        sec = observation("SEC", selection_eligibility="INELIGIBLE")
        yahoo = observation("YAHOO")
        self.assertEqual([x.observation for x in select_effective_observations([sec, yahoo])], [yahoo])

    def test_structural_conflict_does_not_create_a_silent_yahoo_fallback(self):
        sec, yahoo = observation("SEC"), observation("YAHOO", currency="EUR")
        result = select_effective_observations([sec, yahoo])
        self.assertEqual([x.observation for x in result], [sec])
        self.assertEqual(result[0].comparison.comparison_status, "NOT_COMPARED")

    def test_ambiguous_ts_snapshots_do_not_promote_yfinance(self):
        ts = observation("YAHOO")
        conflicting = replace(ts, id=204, raw_id=4, value=Decimal("90"))
        for order in permutations([ts, conflicting, self.yfinance()]):
            self.assertEqual(select_effective_observations(order), [])

    def test_ambiguous_sec_does_not_promote_yahoo(self):
        sec = observation("SEC")
        conflicting = replace(sec, id=104, value=Decimal("90"))
        self.assertEqual(select_effective_observations([sec, conflicting, observation("YAHOO")]), [])

    def test_yahoo_structural_conflict_is_withheld_instead_of_merged(self):
        ts = observation("YAHOO")
        yf = self.yfinance(currency="EUR")
        self.assertEqual([x.observation for x in select_effective_observations([yf, ts])], [ts])

    def test_identical_repeated_input_does_not_create_a_material_tie(self):
        sec = observation("SEC")
        self.assertEqual(select_effective_observations([sec, sec]), select_effective_observations([sec]))

    def test_zero_does_not_hide_sign_conflict_depending_on_input_order(self):
        zero = observation("SEC", "0")
        positive = replace(zero, id=102, raw_id=2, value=Decimal("1"))
        negative = replace(zero, id=103, raw_id=3, value=Decimal("-1"))
        for order in permutations([zero, positive, negative]):
            self.assertEqual(select_effective_observations(order), [])


def effective(source, series_date, value="100", metric="EPS_DILUTED", row_id=1):
    source_variant = "sec.company_facts" if source == "SEC" else (
        "yfinance.quarterly_income_stmt"
        if metric in {"EPS_BASIC", "EPS_UNSPECIFIED"}
        else "yahoo.fundamentals_timeseries"
    )
    normalized = observation(
        source,
        value,
        id=row_id,
        raw_id=row_id,
        metric=metric,
        source_variant=source_variant,
        unit="USD" if metric in {"REVENUE", "NET_INCOME"} else (
            "shares" if metric == "DILUTED_SHARES" else "USD/shares"
        ),
        source_unit="USD" if metric in {"REVENUE", "NET_INCOME"} else (
            "shares" if metric == "DILUTED_SHARES" else "USD/shares"
        ),
        currency=None if metric == "DILUTED_SHARES" else "USD",
        source_metric_name={
            "EPS_DILUTED": "EarningsPerShareDiluted" if source == "SEC" else "quarterlyDilutedEPS",
            "EPS_BASIC": "EarningsPerShareBasic" if source == "SEC" else "Basic EPS",
            "EPS_UNSPECIFIED": "legacy.unknown",
            "REVENUE": "RevenueFromContractWithCustomerExcludingAssessedTax" if source == "SEC" else "quarterlyTotalRevenue",
            "NET_INCOME": "NetIncomeLoss" if source == "SEC" else "quarterlyNetIncome",
            "DILUTED_SHARES": "WeightedAverageNumberOfDilutedSharesOutstanding",
        }[metric],
        source_period_start=(series_date - timedelta(days=90)) if source == "SEC" else None,
        source_period_end=series_date,
        canonical_period_end=series_date,
        series_date=series_date,
        alignment_days=0,
        intrinsic_quality_status=("REVIEW_REQUIRED" if metric == "EPS_UNSPECIFIED" else "OK"),
        selection_eligibility=("INELIGIBLE" if metric == "EPS_UNSPECIFIED" else "ELIGIBLE"),
    )
    return EffectiveObservation(
        normalized,
        "c-v2.6-compatible-v1",
        "TEST_EFFECTIVE",
        ComparisonDiagnostic("NOT_COMPARED", (), None, None, "sec-yahoo-comparison-v1"),
    )


class AnnualComparisonTests(unittest.TestCase):
    current_date = date(2025, 6, 30)
    target_date = current_date - timedelta(days=365)

    def comparisons(self, previous="100"):
        comparable = effective("SEC", self.target_date, previous, row_id=1)
        current_observation = effective("SEC", self.current_date, "120", row_id=2)
        return annual_comparisons_by_source(
            [comparable, current_observation], "EPS_DILUTED",
        )

    def test_exact_annual_target_returns_traced_pair(self):
        result = self.comparisons()

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], AnnualComparison)
        self.assertEqual(result[0].current.observation.id, 2)
        self.assertEqual(result[0].comparable.observation.id, 1)

    def test_annual_window_includes_45_and_excludes_46_days(self):
        for offset in (-45, 45):
            with self.subTest(offset=offset):
                comparable = effective(
                    "SEC", self.target_date + timedelta(days=offset), row_id=1,
                )
                current = effective("SEC", self.current_date, "120", row_id=2)
                self.assertEqual(
                    len(annual_comparisons_by_source(
                        [comparable, current], "EPS_DILUTED",
                    )),
                    1,
                )
        for offset in (-46, 46):
            with self.subTest(offset=offset):
                comparable = effective(
                    "SEC", self.target_date + timedelta(days=offset), row_id=1,
                )
                current = effective("SEC", self.current_date, "120", row_id=2)
                self.assertEqual(
                    annual_comparisons_by_source(
                        [comparable, current], "EPS_DILUTED",
                    ),
                    [],
                )

    def test_never_crosses_source_metric_or_company(self):
        current = effective("SEC", self.current_date, "120", row_id=2)
        mismatches = (
            effective("YAHOO", self.target_date, row_id=1),
            effective("SEC", self.target_date, metric="REVENUE", row_id=3),
            replace(
                effective("SEC", self.target_date, row_id=4),
                observation=replace(
                    effective("SEC", self.target_date, row_id=4).observation,
                    company_id=2,
                ),
            ),
        )
        for comparable in mismatches:
            with self.subTest(comparable=comparable.observation):
                self.assertEqual(
                    annual_comparisons_by_source(
                        [comparable, current], "EPS_DILUTED",
                    ),
                    [],
                )

    def test_metric_argument_filters_the_requested_series(self):
        rows = [
            effective("SEC", self.target_date, row_id=1),
            effective("SEC", self.current_date, "120", row_id=2),
        ]

        self.assertEqual(annual_comparisons_by_source(rows, "REVENUE"), [])

    def test_nearest_selection_is_deterministic_and_order_independent(self):
        current = effective("SEC", self.current_date, "120", row_id=3)
        nearest = effective("SEC", self.target_date - timedelta(days=1), row_id=1)
        farther = effective("SEC", self.target_date - timedelta(days=10), row_id=2)
        rows = [current, nearest, farther]

        expected = annual_comparisons_by_source(rows, "EPS_DILUTED")
        self.assertEqual(
            annual_comparisons_by_source(list(reversed(rows)), "EPS_DILUTED"),
            expected,
        )
        self.assertEqual(expected[0].comparable.observation.id, 1)

    def test_equidistant_material_tie_has_no_comparison(self):
        current = effective("SEC", self.current_date, "120", row_id=3)
        left = effective("SEC", self.target_date - timedelta(days=10), row_id=1)
        right = effective("SEC", self.target_date + timedelta(days=10), row_id=2)

        for rows in ([left, current, right], [right, current, left]):
            self.assertEqual(
                annual_comparisons_by_source(rows, "EPS_DILUTED"), [],
            )

    def test_positive_zero_and_negative_comparables_are_preserved(self):
        for previous in ("100", "0", "-50"):
            with self.subTest(previous=previous):
                comparison = self.comparisons(previous=previous)[0]
                self.assertEqual(
                    comparison.comparable.observation.value, Decimal(previous),
                )
                self.assertEqual(comparison.current.observation.value, Decimal("120"))

    def test_yoy_still_requires_a_strictly_positive_comparable(self):
        cases = (("100", 1), ("0", 0), ("-50", 0))
        for previous, expected_growth_count in cases:
            with self.subTest(previous=previous):
                comparable = effective("SEC", self.target_date, previous, row_id=1)
                current = effective("SEC", self.current_date, "120", row_id=2)
                rows = [comparable, current]
                self.assertEqual(
                    len(annual_comparisons_by_source(rows, "EPS_DILUTED")), 1,
                )
                self.assertEqual(len(growth_yoy_by_source(rows)), expected_growth_count)


class GrowthTests(unittest.TestCase):
    def test_yoy_is_calculated_only_within_each_source(self):
        prior = date(2024, 6, 30)
        current = date(2025, 6, 30)
        for source in ("SEC", "YAHOO"):
            rows = [effective(source, prior, "100", row_id=1), effective(source, current, "120", row_id=2)]
            growth = growth_yoy_by_source(rows)
            self.assertEqual(len(growth), 1)
            self.assertEqual(growth[0].source, source)
            self.assertEqual(growth[0].yoy_pct, Decimal("20"))
        self.assertEqual(growth_yoy_by_source([
            effective("YAHOO", prior, row_id=3), effective("SEC", current, "120", row_id=4)
        ]), [])
        self.assertEqual(growth_yoy_by_source([
            effective("SEC", prior, row_id=5), effective("YAHOO", current, "120", row_id=6)
        ]), [])

    def test_annual_window_exact_45_included_and_46_excluded(self):
        current = date(2025, 6, 30)
        target = current - timedelta(days=365)
        for offset in (0, -45, 45):
            rows = [effective("SEC", target + timedelta(days=offset), row_id=1), effective("SEC", current, "120", row_id=2)]
            self.assertEqual(growth_yoy_by_source(rows)[0].yoy_pct, Decimal("20"))
        for offset in (-46, 46):
            rows = [effective("SEC", target + timedelta(days=offset), row_id=1), effective("SEC", current, "120", row_id=2)]
            self.assertEqual(growth_yoy_by_source(rows), [])

    def test_annual_comparable_is_deterministic_and_material_tie_is_omitted(self):
        current = effective("SEC", date(2025, 6, 30), "120", row_id=3)
        near = effective("SEC", date(2024, 6, 29), "100", row_id=1)
        farther = effective("SEC", date(2024, 6, 20), "90", row_id=2)
        expected = growth_yoy_by_source([current, near, farther])
        self.assertEqual(expected, growth_yoy_by_source([farther, near, current]))
        self.assertEqual(expected[0].comparable.observation.id, 1)
        left = effective("SEC", date(2024, 6, 20), "100", row_id=4)
        right = effective("SEC", date(2024, 7, 10), "100", row_id=5)
        for rows in ((left, current, right), (right, current, left)):
            self.assertEqual(growth_yoy_by_source(rows), [])

    def test_only_c_growth_metrics_are_accepted(self):
        for metric in ("EPS_DILUTED", "REVENUE", "NET_INCOME"):
            rows = [effective("SEC", date(2024, 6, 30), metric=metric, row_id=1), effective("SEC", date(2025, 6, 30), "120", metric, 2)]
            self.assertEqual(len(growth_yoy_by_source(rows)), 1)
        for metric in ("DILUTED_SHARES", "EPS_BASIC", "EPS_UNSPECIFIED"):
            source = "YAHOO" if metric == "EPS_UNSPECIFIED" else "SEC"
            rows = [effective(source, date(2024, 6, 30), metric=metric, row_id=1), effective(source, date(2025, 6, 30), "120", metric, 2)]
            self.assertEqual(growth_yoy_by_source(rows), [])

    def test_legacy_numeric_semantics_require_positive_previous_only(self):
        cases = (
            ("100", "120", Decimal("20")),
            ("100", "-50", Decimal("-150")),
            ("100", "0", Decimal("-100")),
            ("-50", "100", None),
            ("0", "100", None),
        )
        for previous, current, expected in cases:
            with self.subTest(previous=previous, current=current):
                result = growth_yoy_by_source([
                    effective("SEC", date(2024, 6, 30), previous, row_id=1),
                    effective("SEC", date(2025, 6, 30), current, row_id=2),
                ])
                self.assertEqual(None if not result else result[0].yoy_pct, expected)

    def growth(self, current_date, pct, source="SEC", metric="EPS_DILUTED", row_id=10):
        prior = effective(source, current_date - timedelta(days=365), "100", metric, row_id)
        current = effective(source, current_date, str(Decimal("100") + Decimal(str(pct))), metric, row_id + 1)
        return growth_yoy_by_source([prior, current])[0]

    def test_previous_yoy_boundaries_and_outside_window(self):
        previous = self.growth(date(2025, 3, 1), 22, row_id=10)
        for days in (70, 120):
            latest = self.growth(previous.current_series_date + timedelta(days=days), 28, row_id=20)
            trend = growth_acceleration_by_source([latest, previous])[0]
            self.assertIs(trend.previous_yoy, previous)
            self.assertEqual(trend.acceleration_pp, Decimal("6"))
        for days in (69, 121):
            latest = self.growth(previous.current_series_date + timedelta(days=days), 28, row_id=30)
            trend = growth_acceleration_by_source([previous, latest])[0]
            self.assertIsNone(trend.previous_yoy)
            self.assertIsNone(trend.acceleration_pp)

    def test_previous_yoy_never_crosses_source_or_metric(self):
        previous = self.growth(date(2025, 3, 1), 22, row_id=10)
        for latest in (
            self.growth(date(2025, 6, 1), 28, source="YAHOO", row_id=20),
            self.growth(date(2025, 6, 1), 28, metric="REVENUE", row_id=30),
        ):
            trends = growth_acceleration_by_source([previous, latest])
            self.assertTrue(all(item.previous_yoy is None for item in trends))

    def test_acceleration_positive_negative_zero_and_absent(self):
        for previous_pct, latest_pct, expected in ((22, 28, Decimal("6")), (28, 22, Decimal("-6")), (22, 22, Decimal("0"))):
            previous = self.growth(date(2025, 3, 1), previous_pct, row_id=10)
            latest = self.growth(date(2025, 6, 1), latest_pct, row_id=20)
            trend = growth_acceleration_by_source([latest, previous])[0]
            self.assertEqual(trend.acceleration_pp, expected)
        only = self.growth(date(2025, 6, 1), 28, row_id=40)
        self.assertIsNone(growth_acceleration_by_source([only])[0].acceleration_pp)

    def test_growth_and_acceleration_are_deterministic_and_inputs_immutable(self):
        rows = [
            effective("SEC", date(2024, 3, 1), "100", row_id=1),
            effective("SEC", date(2025, 3, 1), "122", row_id=2),
            effective("SEC", date(2024, 6, 1), "100", row_id=3),
            effective("SEC", date(2025, 6, 1), "128", row_id=4),
        ]
        before = deepcopy([asdict(item) for item in rows])
        growth = growth_yoy_by_source(rows)
        self.assertEqual(growth, growth_yoy_by_source(list(reversed(rows))))
        self.assertEqual(growth_acceleration_by_source(growth), growth_acceleration_by_source(list(reversed(growth))))
        self.assertEqual([asdict(item) for item in rows], before)

    def test_traceability_fields_point_to_current_and_comparable(self):
        prior = effective("SEC", date(2024, 6, 30), "100", row_id=7)
        current = effective("SEC", date(2025, 6, 30), "120", row_id=8)
        growth = growth_yoy_by_source([prior, current])[0]
        self.assertIsInstance(growth, YoYGrowth)
        self.assertIs(growth.current, current)
        self.assertIs(growth.comparable, prior)
        self.assertEqual((growth.company_id, growth.metric, growth.source), (1, "EPS_DILUTED", "SEC"))
        self.assertEqual((growth.current_value, growth.comparable_value), (Decimal("120"), Decimal("100")))


if __name__ == "__main__":
    unittest.main()
