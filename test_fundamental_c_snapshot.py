import copy
import hashlib
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd


if "requests" not in sys.modules:
    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    requests.get = None
    sys.modules["requests"] = requests
if "yfinance" not in sys.modules:
    yfinance = types.ModuleType("yfinance")
    yfinance.Ticker = None
    sys.modules["yfinance"] = yfinance

import fundamental_c


DATES = (
    "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    "2025-03-31", "2025-06-30",
)


def _fact(end, value, accession, *, fy=2025, fp="Q1", unit=None):
    end_ts = pd.Timestamp(end)
    start = (end_ts - pd.Timedelta(days=89)).strftime("%Y-%m-%d")
    return {
        "start": start,
        "end": end,
        "val": value,
        "accn": accession,
        "filed": (end_ts + pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
        "form": "10-Q",
        "frame": f"CY{end_ts.year}Q{((end_ts.month - 1) // 3) + 1}",
        "fy": fy,
        "fp": fp,
    }


def _concept(values, tag, unit):
    rows = [
        _fact(date, value, f"{tag}-{index}", fy=pd.Timestamp(date).year)
        for index, (date, value) in enumerate(zip(DATES, values), 1)
    ]
    return {"label": tag, "description": tag, "units": {unit: rows}}


def _companyfacts(*, loss_to_profit=False, unsafe_split=False):
    eps = [1.0, -0.5 if loss_to_profit else 1.1, 1.2, 1.3, 1.5, 1.65]
    shares = [100.0] * len(DATES)
    if unsafe_split:
        shares[-2:] = [200.0, 200.0]
    net_income = [value * shares[index] for index, value in enumerate(eps)]
    return {
        "facts": {"us-gaap": {
            "EarningsPerShareDiluted": _concept(eps, "EarningsPerShareDiluted", "USD/shares"),
            "RevenueFromContractWithCustomerExcludingAssessedTax": _concept(
                [100, 110, 120, 130, 150, 165],
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "USD",
            ),
            "NetIncomeLoss": _concept(net_income, "NetIncomeLoss", "USD"),
            "WeightedAverageNumberOfDilutedSharesOutstanding": _concept(
                shares, "WeightedAverageNumberOfDilutedSharesOutstanding", "shares",
            ),
        }}
    }


class Scenario:
    def __init__(
        self,
        *,
        yahoo=None,
        yahoo_failure=False,
        income=None,
        splits=None,
        splits_error=None,
        loss_to_profit=False,
        unsafe_split=False,
        income_error=None,
        empty_sec=False,
    ):
        self.yahoo = yahoo or {}
        self.yahoo_failure = yahoo_failure
        self.income = pd.DataFrame() if income is None else income
        self.income_error = income_error
        self.splits = pd.Series(dtype="float64") if splits is None else splits
        self.splits_error = splits_error
        self.companyfacts = (
            {"facts": {"us-gaap": {}}}
            if empty_sec
            else _companyfacts(
                loss_to_profit=loss_to_profit,
                unsafe_split=unsafe_split,
            )
        )
        self.trace = []
        self.request_calls = []


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return copy.deepcopy(self.payload)


class FakeTicker:
    def __init__(self, scenario, symbol):
        self.scenario = scenario
        self.symbol = symbol

    def _read(self, name, value):
        self.scenario.trace.append(f"yf:{name}")
        return value

    @property
    def quarterly_income_stmt(self):
        if self.scenario.income_error:
            self.scenario.trace.append("yf:quarterly_income_stmt")
            raise RuntimeError(self.scenario.income_error)
        return self._read("quarterly_income_stmt", self.scenario.income.copy())

    @property
    def sec_filings(self):
        return self._read(
            "sec_filings",
            [{"url": "https://www.sec.gov/Archives/edgar/data/1234567/report.htm"}],
        )

    @property
    def splits(self):
        self.scenario.trace.append("yf:splits")
        if self.scenario.splits_error:
            raise RuntimeError(self.scenario.splits_error)
        return self.scenario.splits.copy()

    @property
    def earnings_history(self):
        return self._read("earnings_history", pd.DataFrame())

    @property
    def eps_estimate(self):
        return self._read("eps_estimate", pd.DataFrame())

    @property
    def eps_revisions(self):
        return self._read("eps_revisions", pd.DataFrame())

    @property
    def growth_estimates(self):
        return self._read("growth_estimates", pd.DataFrame())


class Harness:
    def __init__(self, scenario):
        self.scenario = scenario

    def ticker(self, symbol):
        self.scenario.trace.append(f"yf:Ticker:{symbol}")
        return FakeTicker(self.scenario, symbol)

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.scenario.request_calls.append({
            "url": url,
            "params": copy.deepcopy(params),
            "headers": copy.deepcopy(headers),
            "timeout": timeout,
        })
        if "fundamentals-timeseries" in url:
            series_type = params["type"]
            self.scenario.trace.append(f"yahoo:{series_type}")
            if self.scenario.yahoo_failure:
                raise fundamental_c.requests.RequestException("provider unavailable token=secret")
            rows = [
                {
                    "asOfDate": date,
                    "reportedValue": {"raw": value},
                    "periodType": "3M",
                }
                for date, value in self.scenario.yahoo.get(series_type, [])
            ]
            return FakeResponse({"timeseries": {"result": [{series_type: rows}]}})
        if "companyfacts" in url:
            self.scenario.trace.append("sec:companyfacts")
            return FakeResponse(self.scenario.companyfacts)
        raise AssertionError(f"unexpected request: {url}")

    def patches(self):
        return (
            patch.object(fundamental_c.yf, "Ticker", self.ticker),
            patch.object(fundamental_c.requests, "get", self.get),
            patch.object(
                fundamental_c,
                "_now_naive",
                return_value=pd.Timestamp("2025-10-01"),
            ),
            patch.object(fundamental_c.time, "time", return_value=1759276800),
        )


def _run_public(scenario, ticker="TST"):
    fundamental_c.CIK_CACHE.pop(ticker, None)
    fundamental_c.CIK_MAPPER_CACHE = None
    harness = Harness(scenario)
    patches = harness.patches()
    with patches[0], patches[1], patches[2], patches[3]:
        report = fundamental_c.analyze_current_earnings(ticker)
    return report, list(scenario.trace)


def _run_snapshot(scenario, *, company_id=7, clock=None, ticker="TST"):
    fundamental_c.CIK_CACHE.pop(ticker, None)
    fundamental_c.CIK_MAPPER_CACHE = None
    harness = Harness(scenario)
    patches = harness.patches()
    with patches[0], patches[1], patches[2], patches[3]:
        snapshot = fundamental_c._analyze_current_earnings_snapshot(
            ticker,
            company_id=company_id,
            capture_clock=clock,
        )
    return snapshot, list(scenario.trace)


def _overlap_yahoo(*, fallback=True, comparable=True):
    result = {
        "quarterlyDilutedEPS": [("2025-03-31", 1.5)],
        "quarterlyTotalRevenue": [("2025-03-31", 150.0)],
        "quarterlyNetIncome": [("2025-03-31", 150.0)],
    }
    if fallback:
        result["quarterlyDilutedEPS"].append(("2025-09-30", 1.8))
        result["quarterlyTotalRevenue"].append(("2025-09-30", 180.0))
        result["quarterlyNetIncome"].append(("2025-09-30", 180.0))
    if comparable:
        result["quarterlyDilutedEPS"].insert(0, ("2024-09-30", 1.2))
        result["quarterlyTotalRevenue"].insert(0, ("2024-09-30", 120.0))
        result["quarterlyNetIncome"].insert(0, ("2024-09-30", 120.0))
    return result


EXPECTED_TRACE = [
    "yf:Ticker:TST",
    "yf:quarterly_income_stmt",
    "yahoo:quarterlyDilutedEPS",
    "yahoo:quarterlyTotalRevenue",
    "yahoo:quarterlyNetIncome",
    "yf:sec_filings",
    "sec:companyfacts",
    "yf:splits",
    "yf:earnings_history",
    "yf:eps_estimate",
    "yf:eps_revisions",
    "yf:growth_estimates",
]


class LegacyGoldenMasterTests(unittest.TestCase):
    def test_current_provider_trace_and_full_report_are_stable(self):
        scenario = Scenario(yahoo=_overlap_yahoo(fallback=False))
        report, trace = _run_public(scenario)

        self.assertEqual(trace, EXPECTED_TRACE)
        self.assertEqual(
            scenario.request_calls,
            [
                {
                    "url": "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/TST",
                    "params": {
                        "symbol": "TST",
                        "type": series_type,
                        "period1": 1601164800,
                        "period2": 1759363200,
                        "padTimeSeries": "true",
                        "lang": "en-US",
                        "region": "US",
                    },
                    "headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
                        "Accept": "application/json,text/plain,*/*",
                    },
                    "timeout": 15,
                }
                for series_type in (
                    "quarterlyDilutedEPS",
                    "quarterlyTotalRevenue",
                    "quarterlyNetIncome",
                )
            ]
            + [{
                "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0001234567.json",
                "params": None,
                "headers": fundamental_c.SEC_HEADERS,
                "timeout": 30,
            }],
        )
        self.assertEqual(set(report), {
            "ticker", "version", "data_source", "cik", "cik_source", "error",
            "timeseries_error", "sec_error", "splits_error", "sec_tags",
            "eps_quarters", "revenue_quarters", "net_income_quarters",
            "eps_yoy_pct", "revenue_yoy_pct", "latest_eps", "latest_eps_source",
            "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
            "latest_revenue", "latest_revenue_yoy_pct", "previous_revenue_yoy_pct",
            "revenue_acceleration_pp", "latest_eps_positive", "eps_loss_to_profit",
            "eps_change_type", "earnings_surprises", "latest_eps_surprise_pct",
            "positive_surprises_count", "surprises_available", "eps_estimates",
            "eps_revisions", "growth_estimates", "quarters_eps_available",
            "quarters_revenue_available", "eps_yoy_calculable",
            "revenue_yoy_calculable", "sec_eps_quarters", "sec_revenue_quarters",
            "sec_diluted_shares_quarters", "shares_quality", "yahoo_eps_quarters",
            "yahoo_revenue_quarters", "splits", "split_events",
            "split_integrity_status", "current_yoy_crosses_split",
            "source_consistency", "data_quality", "data_integrity", "c_classic",
            "c_score_v1", "c_flags",
        })
        self.assertEqual(report["latest_eps"], 1.65)
        self.assertAlmostEqual(report["latest_eps_yoy_pct"], 50.0)
        self.assertAlmostEqual(report["previous_eps_yoy_pct"], 50.0)
        self.assertAlmostEqual(report["eps_acceleration_pp"], 0.0)
        self.assertAlmostEqual(report["latest_revenue_yoy_pct"], 50.0)
        self.assertAlmostEqual(report["previous_revenue_yoy_pct"], 50.0)
        self.assertEqual(report["data_integrity"], "VERIFIED")
        self.assertEqual(report["split_integrity_status"], "NO_RECENT_SPLITS")
        self.assertEqual(report["c_classic"]["result"], "PASS_WITH_DECELERATION")
        self.assertEqual(report["c_score_v1"]["status"], "PARTIAL_SCORE")
        self.assertEqual(report["c_score_v1"]["usability"], "C_SCORE_REVIEW")
        self.assertEqual(report["c_score_v1"]["diagnostic"], [
            "EPS_DECELERATING", "SALES_CONFIRM_GROWTH",
        ])
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
        self.assertEqual(
            hashlib.sha256(canonical.encode()).hexdigest(),
            "a1bc757a5863d485c9ea9b9d16398d280de6a88a816dee21e81fd7f4dd2efbfc",
        )

    def test_synthetic_legacy_scenarios_remain_characterized(self):
        cases = [
            (
                "sec_yahoo_overlap_split_safe",
                Scenario(
                    yahoo=_overlap_yahoo(fallback=False),
                    splits=pd.Series({pd.Timestamp("2025-01-15"): 2.0}),
                ),
                "SEC",
                False,
            ),
            ("yahoo_fallback", Scenario(yahoo=_overlap_yahoo()), "YAHOO", False),
            ("missing_comparable", Scenario(yahoo=_overlap_yahoo(comparable=False)), "YAHOO", False),
            ("loss_to_profit", Scenario(loss_to_profit=True), "SEC", True),
            (
                "split_safe",
                Scenario(splits=pd.Series({pd.Timestamp("2025-01-15"): 2.0})),
                "SEC",
                False,
            ),
            (
                "split_unsafe",
                Scenario(
                    splits=pd.Series({pd.Timestamp("2025-01-15"): 2.0}),
                    unsafe_split=True,
                ),
                "SEC",
                False,
            ),
            ("split_unknown", Scenario(splits_error="splits unavailable"), "SEC", False),
            ("partial_provider", Scenario(yahoo_failure=True), "SEC", False),
        ]
        expected_split = {
            "sec_yahoo_overlap_split_safe": "VERIFIED_ALREADY_ADJUSTED",
            "yahoo_fallback": "NO_RECENT_SPLITS",
            "missing_comparable": "NO_RECENT_SPLITS",
            "loss_to_profit": "NO_RECENT_SPLITS",
            "split_safe": "VERIFIED_ALREADY_ADJUSTED",
            "split_unsafe": "UNADJUSTED_DETECTED",
            "split_unknown": "UNKNOWN",
            "partial_provider": "NO_RECENT_SPLITS",
        }
        for name, scenario, source, loss in cases:
            with self.subTest(name=name):
                report, trace = _run_public(scenario)
                self.assertEqual(trace, EXPECTED_TRACE)
                self.assertEqual(report["latest_eps_source"], source)
                self.assertEqual(report["eps_loss_to_profit"], loss)
                self.assertEqual(report["split_integrity_status"], expected_split[name])
                self.assertIn("c_score_v1", report)
                self.assertIn("c_classic", report)


class SnapshotContractTests(unittest.TestCase):
    def test_snapshot_is_transparent_single_capture_and_scores_once(self):
        scenario_public = Scenario(yahoo=_overlap_yahoo())
        public_report, public_trace = _run_public(scenario_public)
        ticks = iter([
            datetime(2025, 10, 1, 12, 0, tzinfo=timezone.utc),
            datetime(2025, 10, 1, 12, 1, tzinfo=timezone.utc),
        ])
        scenario_snapshot = Scenario(yahoo=_overlap_yahoo())
        with patch.object(
            fundamental_c,
            "build_c_score",
            wraps=fundamental_c.build_c_score,
        ) as scorer:
            snapshot, snapshot_trace = _run_snapshot(
                scenario_snapshot,
                clock=lambda: next(ticks),
            )

        self.assertEqual(snapshot["final_report"], public_report)
        self.assertEqual(snapshot_trace, public_trace)
        self.assertEqual(scorer.call_count, 1)
        self.assertEqual(snapshot["capture_metadata"], {
            "legacy_capture_started_at": "2025-10-01T12:00:00+00:00",
            "legacy_capture_completed_at": "2025-10-01T12:01:00+00:00",
        })
        json.dumps(snapshot, sort_keys=True)

    def test_public_wrapper_scores_exactly_once(self):
        scenario = Scenario(yahoo=_overlap_yahoo())
        with patch.object(
            fundamental_c,
            "build_c_score",
            wraps=fundamental_c.build_c_score,
        ) as scorer:
            _run_public(scenario)

        self.assertEqual(scorer.call_count, 1)

    def test_snapshot_contract_and_derived_lineage_are_complete_and_ordered(self):
        snapshot, _ = _run_snapshot(Scenario(yahoo=_overlap_yahoo(fallback=False)))

        self.assertEqual(set(snapshot["fundamental_report"]), {
            "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
            "latest_revenue_yoy_pct", "previous_revenue_yoy_pct",
            "revenue_acceleration_pp", "latest_eps", "eps_yoy_pct",
            "eps_loss_to_profit",
        })
        lineage = snapshot["lineage_by_input"]
        self.assertEqual(len(lineage["latest_eps_yoy_pct"]), 2)
        self.assertEqual(len(lineage["previous_eps_yoy_pct"]), 2)
        self.assertEqual(len(lineage["eps_acceleration_pp"]), 4)
        self.assertEqual(len(lineage["revenue_acceleration_pp"]), 4)
        self.assertEqual(
            len(lineage["eps_yoy_pct"]),
            2 * len(snapshot["fundamental_report"]["eps_yoy_pct"]),
        )
        latest_pair = lineage["latest_eps_yoy_pct"]
        self.assertGreater(latest_pair[0]["source_period_end"], latest_pair[1]["source_period_end"])
        self.assertEqual(lineage["eps_acceleration_pp"][:2], latest_pair)

    def test_sec_lineage_preserves_fact_identity_without_false_normalizer(self):
        snapshot, _ = _run_snapshot(Scenario())
        evidence = snapshot["selected_evidence_by_series"]["sec_eps"][-1]

        self.assertEqual(evidence["company_id"], 7)
        self.assertEqual(evidence["source"], "SEC")
        self.assertEqual(evidence["source_variant"], "sec.company_facts")
        self.assertEqual(evidence["source_metric_name"], "EarningsPerShareDiluted")
        self.assertEqual(evidence["unit"], "USD/shares")
        self.assertEqual(evidence["source_record_id"], "EarningsPerShareDiluted-6")
        self.assertEqual(evidence["form"], "10-Q")
        self.assertIsNotNone(evidence["frame"])
        self.assertIsNotNone(evidence["fiscal_year"])
        self.assertIsNone(evidence["origin_normalizer_version"])
        self.assertNotIn("normalizer_version", evidence)

    def test_sec_filed_tie_keeps_numeric_dedup_but_not_a_false_accession(self):
        scenario = Scenario()
        rows = scenario.companyfacts["facts"]["us-gaap"][
            "EarningsPerShareDiluted"
        ]["units"]["USD/shares"]
        tied = copy.deepcopy(rows[-1])
        tied["accn"] = "different-accession-same-fact"
        rows.append(tied)

        snapshot, _ = _run_snapshot(scenario)
        evidence = snapshot["selected_evidence_by_series"]["sec_eps"][-1]

        self.assertEqual(snapshot["final_report"]["latest_eps"], 1.65)
        self.assertIsNone(evidence["source_record_id"])
        self.assertEqual(evidence["identity_ambiguity"], "SEC_FILED_TIE")

    def test_sec_filed_tie_with_conflicting_values_keeps_identity_ambiguous(self):
        scenario = Scenario()
        rows = scenario.companyfacts["facts"]["us-gaap"][
            "EarningsPerShareDiluted"
        ]["units"]["USD/shares"]
        tied = copy.deepcopy(rows[-1])
        tied["accn"] = "conflicting-accession"
        tied["val"] = 9.99
        rows.append(tied)

        snapshot, _ = _run_snapshot(scenario)
        evidence = snapshot["selected_evidence_by_series"]["sec_eps"][-1]

        self.assertEqual(snapshot["final_report"]["latest_eps"], 9.99)
        self.assertIsNone(evidence["source_record_id"])
        self.assertEqual(evidence["identity_ambiguity"], "SEC_FILED_TIE")

    def test_yahoo_lineage_preserves_only_explicit_metadata(self):
        snapshot, _ = _run_snapshot(Scenario(yahoo=_overlap_yahoo()))
        evidence = snapshot["selected_evidence_by_series"]["yahoo_eps"][-1]

        self.assertEqual(evidence["source_variant"], "yahoo.fundamentals_timeseries")
        self.assertEqual(evidence["source_metric_name"], "quarterlyDilutedEPS")
        self.assertEqual(evidence["source_period_end"], "2025-09-30")
        self.assertEqual(evidence["raw_value"], 1.8)
        self.assertIsNone(evidence["fiscal_year"])
        self.assertIsNone(evidence["fiscal_quarter"])
        self.assertNotIn("immutable_revision_id", evidence)
        self.assertIsNone(evidence["currency"])
        self.assertIsNone(evidence["unit"])

    def test_yfinance_basic_eps_alias_is_never_relabelled_diluted(self):
        income = pd.DataFrame(
            {pd.Timestamp("2025-09-30"): [1.8, 180.0, 180.0]},
            index=["Basic EPS", "Total Revenue", "Net Income"],
        )
        snapshot, _ = _run_snapshot(Scenario(income=income))
        evidence = snapshot["selected_evidence_by_series"]["yahoo_eps"][-1]

        self.assertEqual(evidence["source_variant"], "yfinance.quarterly_income_stmt")
        self.assertEqual(evidence["source_metric_name"], "Basic EPS")
        self.assertEqual(evidence["metric"], "EPS_BASIC")

    def test_missing_comparable_keeps_incomplete_lineage_honest(self):
        snapshot, _ = _run_snapshot(
            Scenario(yahoo=_overlap_yahoo(comparable=False)),
        )

        self.assertEqual(len(snapshot["lineage_by_input"]["latest_eps"]), 1)
        self.assertEqual(len(snapshot["lineage_by_input"]["eps_loss_to_profit"]), 1)

    def test_provider_failure_status_complements_score_and_semantic_gaps(self):
        snapshot, _ = _run_snapshot(Scenario(yahoo_failure=True))

        self.assertEqual(snapshot["acquisition_status"], "PARTIAL")
        self.assertTrue(snapshot["provider_failures"])
        self.assertNotIn("secret", json.dumps(snapshot["provider_failures"]))
        self.assertEqual(snapshot["shared_live_complements"], {
            "data_integrity": snapshot["final_report"]["data_integrity"],
            "split_integrity_status": snapshot["final_report"]["split_integrity_status"],
        })
        self.assertEqual(snapshot["legacy_end_to_end_score"], {
            "c_classic": snapshot["final_report"]["c_classic"],
            "c_score_v1": snapshot["final_report"]["c_score_v1"],
            "c_flags": snapshot["final_report"]["c_flags"],
        })
        codes = {item["code"] for item in snapshot["semantic_gaps"]}
        self.assertIn("CORPORATE_ACTIONS_NOT_RECONSTRUCTED", codes)
        self.assertIn("DATA_INTEGRITY_NOT_RECONSTRUCTED", codes)

    def test_cik_cache_semantics_are_unchanged(self):
        first = Scenario()
        _run_snapshot(first)
        second = Scenario()
        harness = Harness(second)
        patches = harness.patches()
        with patches[0], patches[1], patches[2], patches[3]:
            fundamental_c._analyze_current_earnings_snapshot("TST")

        self.assertNotIn("yf:sec_filings", second.trace)
        self.assertEqual(fundamental_c.CIK_CACHE["TST"], "0001234567")

    def test_cik_mapper_cache_is_reused_without_request(self):
        fundamental_c.CIK_CACHE.pop("MAP", None)
        fundamental_c.CIK_MAPPER_CACHE = {"MAP": "7654321"}
        with patch.object(
            fundamental_c.requests,
            "get",
            side_effect=AssertionError("cached mapper must not request"),
        ):
            cik = fundamental_c._cik_from_sec_cik_mapper("MAP")

        self.assertEqual(cik, "0007654321")

    def test_no_usable_fundamentals_with_provider_failures_is_failed(self):
        snapshot, _ = _run_snapshot(Scenario(
            yahoo_failure=True,
            income_error="income unavailable",
            empty_sec=True,
        ))

        self.assertEqual(snapshot["acquisition_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
