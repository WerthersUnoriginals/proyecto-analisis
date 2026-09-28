import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.c_data_integrity import (
    LEGACY_CONSISTENCY_VERSION,
    LegacyConsistencyResult,
    aggregate_data_integrity,
    compare_legacy_source_consistency,
    classify_data_quality,
    load_normalized_evidence,
    reconstruct_persisted_integrity,
)


UTC = timezone.utc


def obs(value, day, *, source="SEC", variant="sec.company_facts", metric="REVENUE",
        name=None, observed=None, raw_id=1):
    return {
        "value": Decimal(str(value)),
        "source_period_end": date.fromisoformat(day),
        "series_date": date.fromisoformat(day),
        "source": source,
        "source_variant": variant,
        "metric": metric,
        "source_metric_name": name,
        "observed_at": observed or datetime(2026, 1, 1, tzinfo=UTC),
        "raw_id": raw_id,
    }


class LegacyConsistencyTests(unittest.TestCase):
    def test_exact_formula_and_threshold_boundaries(self):
        sec = [obs("100", "2025-01-01")]
        for value, status in (
            ("99", "OK"),
            ("98.999", "DISCREPANCIA_CONTABLE"),
            ("95", "DISCREPANCIA_CONTABLE"),
            ("94.999", "DISCREPANCIA_ALTA"),
        ):
            with self.subTest(value=value):
                result = compare_legacy_source_consistency(
                    sec, [obs(value, "2025-01-01", source="YAHOO", variant="yahoo.fundamentals_timeseries")],
                )
                self.assertEqual(result.status, status)
        self.assertAlmostEqual(
            compare_legacy_source_consistency(
                sec, [obs("99", "2025-01-01", source="YAHOO", variant="yahoo.fundamentals_timeseries")],
            ).max_diff_pct,
            1.0,
        )

    def test_zero_signs_and_no_overlap(self):
        def compare(left, right):
            return compare_legacy_source_consistency(
                [obs(left, "2025-01-01")],
                [obs(right, "2025-01-01", source="YAHOO", variant="yahoo.fundamentals_timeseries")],
            )

        self.assertEqual(compare(0, 0).status, "OK")
        self.assertEqual(compare(0, 1).max_diff_pct, 100.0)
        self.assertEqual(compare(1, -1).max_diff_pct, 200.0)
        self.assertEqual(compare(100, 110).max_diff_pct, 100 * 10 / 110)
        self.assertEqual(
            compare_legacy_source_consistency(
                [obs(100, "2025-01-01")],
                [obs(100, "2025-02-05", source="YAHOO", variant="yahoo.fundamentals_timeseries")],
            ).matched_count,
            1,
        )
        self.assertEqual(
            compare_legacy_source_consistency(
                [obs(100, "2025-01-01")],
                [obs(100, "2025-02-06", source="YAHOO", variant="yahoo.fundamentals_timeseries")],
            ).status,
            "N/D",
        )

    def test_maximum_dominates_and_sec_can_be_reused(self):
        result = compare_legacy_source_consistency(
            [obs(100, "2025-01-01")],
            [
                obs(100, "2025-01-01", source="YAHOO", variant="yahoo.fundamentals_timeseries", raw_id=2),
                obs(90, "2025-01-02", source="YAHOO", variant="yahoo.fundamentals_timeseries", raw_id=3),
            ],
        )
        self.assertEqual(result.matched_count, 2)
        self.assertAlmostEqual(result.max_diff_pct, 10.0)
        self.assertAlmostEqual(result.avg_diff_pct, 5.0)

    def test_conflicting_nearest_tie_fails_closed_without_demonstrated_order(self):
        result = compare_legacy_source_consistency(
            [obs(100, "2025-01-01", raw_id=1), obs(120, "2025-01-03", raw_id=2)],
            [obs(100, "2025-01-02", source="YAHOO", variant="yahoo.fundamentals_timeseries")],
        )
        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertIn("SEC_NEAREST_TIE", result.reasons)

    def test_demonstrated_legacy_order_resolves_nearest_tie(self):
        result = compare_legacy_source_consistency(
            [obs(100, "2025-01-01", raw_id=1), obs(120, "2025-01-03", raw_id=2)],
            [obs(100, "2025-01-02", source="YAHOO", variant="yahoo.fundamentals_timeseries")],
            candidate_order_demonstrated=True,
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.matches[0].sec_value, Decimal("100"))

    def test_eps_semantics_are_not_silently_equated(self):
        sec = [obs(2, "2025-01-01", metric="EPS_DILUTED", name="EarningsPerShareDiluted")]
        basic = [obs(2, "2025-01-01", source="YAHOO", variant="yfinance.quarterly_income_stmt",
                     metric="EPS_BASIC", name="Basic EPS")]
        unspecified = [obs(2, "2025-01-01", source="YAHOO", variant="yfinance.quarterly_income_stmt",
                           metric="EPS_UNSPECIFIED", name="legacy.unknown")]
        self.assertEqual(compare_legacy_source_consistency(sec, basic, metric="EPS").status, "AMBIGUOUS")
        self.assertEqual(compare_legacy_source_consistency(sec, unspecified, metric="EPS").status, "AMBIGUOUS")

    def test_yahoo_timeseries_precedes_yfinance_and_fallback_is_explicit(self):
        sec = [obs(100, "2025-01-01")]
        yahoo = [
            obs(100, "2025-01-01", source="YAHOO", variant="yahoo.fundamentals_timeseries", raw_id=2),
            obs(90, "2025-01-01", source="YAHOO", variant="yfinance.quarterly_income_stmt", raw_id=3),
        ]
        result = compare_legacy_source_consistency(sec, yahoo)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.matched_count, 1)

    def test_modern_comparison_status_is_not_consulted(self):
        result = compare_legacy_source_consistency(
            [obs(100, "2025-01-01")],
            [dict(obs(100, "2025-01-01", source="YAHOO", variant="yahoo.fundamentals_timeseries"),
                  comparison_status="REVIEW_REQUIRED_HIGH")],
        )
        self.assertEqual(result.status, "OK")


class LoaderTests(unittest.TestCase):
    def test_loader_exposes_sec_accession_from_joined_raw_evidence(self):
        class Cursor:
            description = [
                type("D", (), {"name": name})()
                for name in (
                    "id", "source", "source_variant", "raw_id", "observed_at",
                    "raw_source_record_id", "raw_payload_accn",
                )
            ]

            def __init__(self):
                self.calls = []

            def execute(self, query, params):
                self.calls.append((query, params))

            def fetchall(self):
                return [(
                    7, "SEC", "sec.company_facts", 99,
                    datetime(2025, 1, 1, tzinfo=UTC),
                    "0000320193-25-000057", "0000320193-25-000057",
                )]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def cursor(self):
                return self.cursor_obj

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        connection = Connection()
        rows = load_normalized_evidence(
            1, datetime(2025, 2, 1, tzinfo=UTC), lambda: connection,
        )
        self.assertEqual(rows[0]["source_record_id"], "0000320193-25-000057")
        self.assertEqual(rows[0]["source_identity_type"], "SEC_ACCESSION")
        self.assertNotIn("raw_id_as_accession", rows[0])
        query, params = connection.cursor_obj.calls[0]
        self.assertIn("JOIN FUNDAMENTALS_RAW", query.upper())
        self.assertEqual(len(connection.cursor_obj.calls), 1)
        self.assertEqual(params[0], 1)

    def test_loader_with_inconsistent_sec_raw_identity_fails_closed(self):
        class Cursor:
            description = [
                type("D", (), {"name": name})()
                for name in (
                    "id", "source", "source_variant", "raw_id", "observed_at",
                    "raw_source_record_id", "raw_payload_accn",
                )
            ]

            def execute(self, query, params):
                pass

            def fetchall(self):
                return [(
                    7, "SEC", "sec.company_facts", 99,
                    datetime(2025, 1, 1, tzinfo=UTC),
                    "0000320193-25-000057", "0000320193-25-000058",
                )]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        rows = load_normalized_evidence(
            1, datetime(2025, 2, 1, tzinfo=UTC), lambda: Connection(),
        )
        self.assertIsNone(rows[0]["source_record_id"])
        self.assertIsNone(rows[0]["source_identity_type"])
        self.assertEqual(rows[0]["source_identity_error"], "SEC_ACCESSION_MISMATCH")

    def test_loader_with_missing_sec_accession_fails_closed(self):
        class Cursor:
            description = [
                type("D", (), {"name": name})()
                for name in (
                    "id", "source", "source_variant", "raw_id", "observed_at",
                    "raw_source_record_id", "raw_payload_accn",
                )
            ]

            def execute(self, query, params):
                pass

            def fetchall(self):
                return [(
                    7, "SEC", "sec.company_facts", 99,
                    datetime(2025, 1, 1, tzinfo=UTC),
                    None, None,
                )]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        rows = load_normalized_evidence(
            1, datetime(2025, 2, 1, tzinfo=UTC), lambda: Connection(),
        )
        self.assertIsNone(rows[0]["source_record_id"])
        self.assertIsNone(rows[0]["source_identity_type"])
        self.assertEqual(rows[0]["source_identity_error"], "SEC_ACCESSION_MISSING")

    def test_loader_uses_one_normalized_query_and_filters_as_of(self):
        class Cursor:
            description = [type("D", (), {"name": "id"})(), type("D", (), {"name": "observed_at"})()]

            def __init__(self):
                self.calls = []

            def execute(self, query, params):
                self.calls.append((query, params))

            def fetchall(self):
                return [
                    (1, datetime(2025, 1, 1, tzinfo=UTC)),
                    (2, datetime(2025, 3, 1, tzinfo=UTC)),
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def cursor(self):
                return self.cursor_obj

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        connection = Connection()
        rows = load_normalized_evidence(1, datetime(2025, 2, 1, tzinfo=UTC), lambda: connection)
        self.assertEqual(rows, ({"id": 1, "observed_at": datetime(2025, 1, 1, tzinfo=UTC)},))
        self.assertEqual(len(connection.cursor_obj.calls), 1)
        query, params = connection.cursor_obj.calls[0]
        self.assertIn("FROM fundamentals_normalized", query)
        self.assertNotIn("fundamentals_effective_current", query)
        self.assertEqual(params[0], 1)

    def test_as_of_hides_later_sec_amendment_and_yahoo_observation(self):
        class Cursor:
            description = [
                type("D", (), {"name": "id"})(),
                type("D", (), {"name": "source"})(),
                type("D", (), {"name": "observed_at"})(),
            ]

            def execute(self, query, params):
                self.params = params

            def fetchall(self):
                return [
                    (1, "SEC", datetime(2025, 1, 1, tzinfo=UTC)),
                    (2, "SEC", datetime(2025, 2, 1, tzinfo=UTC)),
                    (3, "YAHOO", datetime(2025, 3, 1, tzinfo=UTC)),
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def cursor(self):
                return self.cursor_obj

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        rows = load_normalized_evidence(
            1, datetime(2025, 1, 15, tzinfo=UTC), lambda: Connection(),
        )
        self.assertEqual([row["id"] for row in rows], [1])


class DataQualityTests(unittest.TestCase):
    def test_all_data_quality_states_and_split_override(self):
        complete = dict(
            latest_eps_yoy_pct=1, previous_eps_yoy_pct=2, eps_acceleration_pp=3,
            latest_revenue_yoy_pct=4, previous_revenue_yoy_pct=5, revenue_acceleration_pp=6,
        )
        self.assertEqual(classify_data_quality(**complete), "completa")
        self.assertEqual(classify_data_quality(**{**complete, "previous_eps_yoy_pct": None}), "suficiente")
        self.assertEqual(classify_data_quality(**{**complete, "latest_revenue_yoy_pct": None}), "parcial")
        self.assertEqual(classify_data_quality(**{**complete, "latest_eps_yoy_pct": None, "latest_revenue_yoy_pct": None}), "insuficiente")
        self.assertEqual(classify_data_quality(**complete, current_yoy_crosses_split=True, split_integrity_status="REVIEW_REQUIRED"), "revision_split")

    def test_data_integrity_precedence_and_warning_order(self):
        ok = {name: LegacyConsistencyResult("N/D", None, None, 0) for name in ("EPS", "REVENUE", "NET_INCOME")}
        self.assertEqual(aggregate_data_integrity("completa", "NO_RECENT_SPLITS", ok, "DILUTED_EXACT").data_integrity, "VERIFIED")
        warning = dict(ok)
        warning["EPS"] = LegacyConsistencyResult("DISCREPANCIA_CONTABLE", 2, 2, 1)
        result = aggregate_data_integrity("parcial", "NO_RECENT_SPLITS", warning, "BASIC_FALLBACK")
        self.assertEqual(result.data_integrity, "VERIFIED_WITH_ACCOUNTING_DIFFERENCE_AND_BASIC_SHARES_FALLBACK_AND_PARTIAL_CORE_DATA")
        self.assertEqual(result.warnings, ("ACCOUNTING_DIFFERENCE", "BASIC_SHARES_FALLBACK", "PARTIAL_CORE_DATA"))
        self.assertEqual(
            aggregate_data_integrity(
                "suficiente", "NO_RECENT_SPLITS", ok, "NOT_AVAILABLE",
            ).data_integrity,
            "VERIFIED_WITH_PARTIAL_CORE_DATA",
        )
        high = dict(ok)
        high["REVENUE"] = LegacyConsistencyResult("DISCREPANCIA_ALTA", 6, 6, 1)
        self.assertEqual(aggregate_data_integrity("completa", "NO_RECENT_SPLITS", high, "DILUTED_EXACT").data_integrity, "REVIEW_REQUIRED")
        ambiguous = dict(ok)
        ambiguous["EPS"] = LegacyConsistencyResult("AMBIGUOUS", None, None, 0, reasons=("EPS_SEMANTICS_UNPROVEN",))
        self.assertEqual(aggregate_data_integrity("completa", "NO_RECENT_SPLITS", ambiguous, "DILUTED_EXACT").data_integrity, "REVIEW_REQUIRED")
        self.assertEqual(aggregate_data_integrity("insuficiente", "NO_RECENT_SPLITS", ok, "DILUTED_EXACT").data_integrity, "REVIEW_REQUIRED")
        self.assertEqual(aggregate_data_integrity("revision_split", "NO_RECENT_SPLITS", ok, "DILUTED_EXACT").data_integrity, "REVIEW_REQUIRED")
        for split_status in ("UNADJUSTED_DETECTED", "REVIEW_REQUIRED", "UNKNOWN"):
            with self.subTest(split_status=split_status):
                self.assertEqual(aggregate_data_integrity("completa", split_status, ok, "DILUTED_EXACT").data_integrity, "REVIEW_REQUIRED")

    def test_data_integrity_matches_legacy_quality_matrix(self):
        consistent = {
            name: LegacyConsistencyResult("N/D", None, None, 0)
            for name in ("EPS", "REVENUE", "NET_INCOME")
        }
        expected = {
            "completa": "VERIFIED",
            "suficiente": "VERIFIED_WITH_PARTIAL_CORE_DATA",
            "parcial": "VERIFIED_WITH_PARTIAL_CORE_DATA",
            "insuficiente": "REVIEW_REQUIRED",
            "revision_split": "REVIEW_REQUIRED",
        }
        for quality, result in expected.items():
            with self.subTest(quality=quality):
                self.assertEqual(
                    aggregate_data_integrity(
                        quality, "NO_RECENT_SPLITS", consistent, "DILUTED_EXACT",
                    ).data_integrity,
                    result,
                )

    def test_data_integrity_domain_includes_all_three_warning_combination(self):
        consistency = {
            "EPS": LegacyConsistencyResult("DISCREPANCIA_CONTABLE", 2, 2, 1),
            "REVENUE": LegacyConsistencyResult("N/D", None, None, 0),
            "NET_INCOME": LegacyConsistencyResult("N/D", None, None, 0),
        }
        result = aggregate_data_integrity(
            "parcial", "NO_RECENT_SPLITS", consistency, "BASIC_FALLBACK",
        )
        self.assertEqual(
            result.data_integrity,
            "VERIFIED_WITH_ACCOUNTING_DIFFERENCE_AND_BASIC_SHARES_FALLBACK_AND_PARTIAL_CORE_DATA",
        )

    def test_composition_consistency_uses_latest_filing_not_latest_ingestion(self):
        result = reconstruct_persisted_integrity(
            self._composition_rows(
                older_value=80, newer_value=100, yahoo_value=100,
                older_observed=datetime(2025, 4, 1, tzinfo=UTC),
                newer_observed=datetime(2025, 3, 1, tzinfo=UTC),
            ),
            (self._complete_capture(),),
            {1: ()},
            as_of=datetime(2025, 5, 1, tzinfo=UTC),
            scalars=self._complete_scalars(),
        )
        self.assertEqual(result.consistency["REVENUE"].status, "OK")
        self.assertEqual(result.consistency["REVENUE"].matches[0].sec_value, Decimal("100"))

    def test_composition_latest_filing_difference_overrides_matching_older_filing(self):
        result = reconstruct_persisted_integrity(
            self._composition_rows(
                older_value=100, newer_value=80, yahoo_value=100,
                older_observed=datetime(2025, 4, 1, tzinfo=UTC),
                newer_observed=datetime(2025, 3, 1, tzinfo=UTC),
            ),
            (self._complete_capture(),),
            {1: ()},
            as_of=datetime(2025, 5, 1, tzinfo=UTC),
            scalars=self._complete_scalars(),
        )
        self.assertEqual(result.consistency["REVENUE"].status, "DISCREPANCIA_ALTA")
        self.assertEqual(result.consistency["REVENUE"].matches[0].sec_value, Decimal("80"))

    def test_composition_ambiguous_latest_filing_fails_closed(self):
        rows = self._composition_rows(
            older_value=80, newer_value=100, yahoo_value=100,
            older_observed=datetime(2025, 3, 1, tzinfo=UTC),
            newer_observed=datetime(2025, 4, 1, tzinfo=UTC),
        )
        conflicting = dict(rows[1], value=Decimal("120"), raw_id=3)
        rows.insert(2, conflicting)
        result = reconstruct_persisted_integrity(
            rows,
            (self._complete_capture(),),
            {1: ()},
            as_of=datetime(2025, 5, 1, tzinfo=UTC),
            scalars=self._complete_scalars(),
        )
        self.assertEqual(result.consistency["REVENUE"].status, "AMBIGUOUS")
        self.assertIn("SEC_LATEST_FILING_AMBIGUOUS", result.consistency["REVENUE"].reasons)
        self.assertEqual(result.data_integrity, "REVIEW_REQUIRED")

    def test_composition_superseded_duplicate_is_not_counted(self):
        rows = self._composition_rows(
            older_value=75, newer_value=100, yahoo_value=100,
            older_observed=datetime(2025, 4, 1, tzinfo=UTC),
            newer_observed=datetime(2025, 3, 1, tzinfo=UTC),
        )
        result = reconstruct_persisted_integrity(
            rows,
            (self._complete_capture(),),
            {1: ()},
            as_of=datetime(2025, 5, 1, tzinfo=UTC),
            scalars=self._complete_scalars(),
        )
        self.assertEqual(result.consistency["REVENUE"].matched_count, 1)
        self.assertEqual(len(result.consistency["REVENUE"].matches), 1)

    @staticmethod
    def _complete_capture():
        return type("Capture", (), {
            "id": 1,
            "observed_at": datetime(2025, 1, 1, tzinfo=UTC),
            "acquisition_status": "SUCCESS",
            "completeness_status": "COMPLETE",
        })()

    @staticmethod
    def _complete_scalars():
        return {
            "latest_eps_yoy_pct": 10,
            "previous_eps_yoy_pct": 9,
            "eps_acceleration_pp": 1,
            "latest_revenue_yoy_pct": 8,
            "previous_revenue_yoy_pct": 7,
            "revenue_acceleration_pp": 1,
            "previous_eps_period": date(2024, 1, 1),
            "latest_eps_period": date(2025, 1, 1),
        }

    @staticmethod
    def _composition_rows(
        *, older_value, newer_value, yahoo_value, older_observed, newer_observed,
    ):
        period = date(2024, 12, 31)
        base = {
            "source": "SEC",
            "source_variant": "sec.company_facts",
            "metric": "REVENUE",
            "source_metric_name": "Revenues",
            "source_period_end": period,
            "series_date": period,
            "normalizer_version": "sec-normalized-v2",
            "selection_eligibility": "ELIGIBLE",
        }
        return [
            {
                **base,
                "value": Decimal(str(older_value)),
                "filed_date": date(2025, 1, 15),
                "observed_at": older_observed,
                "raw_id": 1,
            },
            {
                **base,
                "value": Decimal(str(newer_value)),
                "filed_date": date(2025, 2, 15),
                "observed_at": newer_observed,
                "raw_id": 2,
            },
            {
                "value": Decimal(str(yahoo_value)),
                "source_period_end": period,
                "series_date": period,
                "source": "YAHOO",
                "source_variant": "yahoo.fundamentals_timeseries",
                "metric": "REVENUE",
                "source_metric_name": "quarterlyTotalRevenue",
                "observed_at": datetime(2025, 3, 15, tzinfo=UTC),
                "raw_id": 10,
            },
        ]

    def test_persisted_composition_returns_structured_independent_result(self):
        class Capture:
            id = 1
            observed_at = datetime(2025, 1, 1, tzinfo=UTC)
            acquisition_status = "SUCCESS"
            completeness_status = "COMPLETE"

        scalars = {
            "latest_eps_yoy_pct": 10, "previous_eps_yoy_pct": 9, "eps_acceleration_pp": 1,
            "latest_revenue_yoy_pct": 8, "previous_revenue_yoy_pct": 7, "revenue_acceleration_pp": 1,
            "previous_eps_period": date(2024, 1, 1), "latest_eps_period": date(2025, 1, 1),
        }
        result = reconstruct_persisted_integrity(
            (), (Capture(),), {1: ()}, as_of=datetime(2025, 2, 1, tzinfo=UTC), scalars=scalars,
        )
        self.assertEqual(result.data_quality, "completa")
        self.assertEqual(result.split_integrity_status, "NO_RECENT_SPLITS")
        self.assertEqual(result.data_integrity, "VERIFIED")
        self.assertEqual(set(result.consistency), {"EPS", "REVENUE", "NET_INCOME"})


if __name__ == "__main__":
    unittest.main()
