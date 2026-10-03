"""Offline tests for v3 quarterly normalization, split basis, Q4 and growth."""

import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from database.quarterly_v3 import (
    QuarterKey,
    build_fiscal_calendar,
    build_quarterly_view,
    yahoo_rows_from_snapshot,
)
from database.sec_facts import SecFact, parse_companyfacts
from database.split_basis import SplitEvent

UTC = timezone.utc
FIXTURES = Path(__file__).resolve().parent / "fixtures"
OBSERVED = datetime(2026, 10, 2, tzinfo=UTC)


def real_facts(ticker):
    payload = json.loads((FIXTURES / f"sec_companyfacts_{ticker}_2026-10-02.json").read_text(encoding="utf-8"))
    return [
        SecFact(**{**fact.__dict__, "observed_at": OBSERVED, "id": index})
        for index, fact in enumerate(parse_companyfacts(payload), start=1)
    ]


_ids = iter(range(10_000, 99_999))


def fact(tag, start, end, value, *, accession, filed, fy, fp, form="10-Q", unit=None):
    unit = unit or ("USD/shares" if "PerShare" in tag else "shares" if "Shares" in tag else "USD")
    return SecFact(
        taxonomy="us-gaap", tag=tag, unit=unit,
        period_start=start, period_end=end, value=Decimal(value),
        accession=accession, fiscal_year=fy, fiscal_period=fp, form=form,
        filed_date=filed, frame=None, observed_at=OBSERVED, id=next(_ids),
    )


def yahoo(metric, name, end, value, variant="yahoo.fundamentals_timeseries", observed=OBSERVED, raw_id=None):
    return {
        "id": raw_id or next(_ids), "metric": metric, "source_metric_name": name,
        "source_variant": variant, "period_end": end, "value": Decimal(value),
        "observed_at": observed,
    }


def calendar_year_company(eps_by_quarter, *, revenue=None):
    """Quarterly 10-Q facts plus a 10-K with annual and no Q4 quarterly facts."""
    facts = []
    for (fy, q), eps in eps_by_quarter.items():
        start = date(fy, 3 * q - 2, 1)
        end = date(fy, 3 * q, 30 if q in (2, 3) else 31)
        filed = date(fy, 3 * q + 1, 30) if q < 4 else date(fy + 1, 2, 20)
        if q == 4:
            continue
        facts.append(fact("EarningsPerShareDiluted", start, end, eps,
                          accession=f"q{fy}{q}", filed=filed, fy=fy, fp=f"Q{q}"))
        if revenue:
            facts.append(fact("Revenues", start, end, revenue[(fy, q)],
                              accession=f"q{fy}{q}", filed=filed, fy=fy, fp=f"Q{q}"))
    return facts


class FiscalCalendarTests(unittest.TestCase):
    def test_ten_q_fiscal_period_identifies_quarter(self):
        facts = [fact("NetIncomeLoss", date(2024, 1, 1), date(2024, 3, 31), "1",
                      accession="a", filed=date(2024, 5, 1), fy=2024, fp="Q1")]
        calendar = build_fiscal_calendar(facts)
        identity = calendar.quarters[(date(2024, 1, 1), date(2024, 3, 31))]
        self.assertEqual(identity.key, QuarterKey(2024, 1))

    def test_later_comparative_cannot_reassign_identity(self):
        facts = [
            fact("NetIncomeLoss", date(2024, 1, 1), date(2024, 3, 31), "1",
                 accession="a", filed=date(2024, 5, 1), fy=2024, fp="Q1"),
            fact("NetIncomeLoss", date(2024, 1, 1), date(2024, 3, 31), "1",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
            fact("NetIncomeLoss", date(2025, 1, 1), date(2025, 3, 31), "2",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
        ]
        calendar = build_fiscal_calendar(facts)
        self.assertEqual(calendar.quarters[(date(2024, 1, 1), date(2024, 3, 31))].key, QuarterKey(2024, 1))
        self.assertEqual(calendar.quarters[(date(2025, 1, 1), date(2025, 3, 31))].key, QuarterKey(2025, 1))

    def test_comparative_without_original_filing_is_located_by_projection(self):
        facts = [
            fact("NetIncomeLoss", date(2024, 1, 1), date(2024, 3, 31), "1",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
            fact("NetIncomeLoss", date(2025, 1, 1), date(2025, 3, 31), "2",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
        ]
        identity = build_fiscal_calendar(facts).quarters[(date(2024, 1, 1), date(2024, 3, 31))]
        self.assertEqual(identity.key, QuarterKey(2024, 1))

    def test_ten_k_quarter_is_q4_only_when_it_ends_with_the_annual_fact(self):
        facts = [
            fact("NetIncomeLoss", date(2024, 1, 1), date(2024, 12, 31), "10",
                 accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"),
            fact("NetIncomeLoss", date(2024, 10, 1), date(2024, 12, 31), "3",
                 accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"),
            fact("NetIncomeLoss", date(2024, 4, 1), date(2024, 6, 30), "2",
                 accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"),
        ]
        calendar = build_fiscal_calendar(facts)
        self.assertEqual(calendar.quarters[(date(2024, 10, 1), date(2024, 12, 31))].key, QuarterKey(2024, 4))
        # First seen in a 10-K quarterly table: located inside the annual interval, not labeled Q4.
        self.assertEqual(calendar.quarters[(date(2024, 4, 1), date(2024, 6, 30))].key, QuarterKey(2024, 2))

    def test_quarter_without_identity_evidence_is_unresolved(self):
        facts = [fact("NetIncomeLoss", date(2024, 4, 1), date(2024, 6, 30), "2",
                      accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K")]
        identity = build_fiscal_calendar(facts).quarters[(date(2024, 4, 1), date(2024, 6, 30))]
        self.assertIsNone(identity.key)
        self.assertIn("FISCAL_IDENTITY_UNRESOLVED", identity.reasons)


class Q4DerivationTests(unittest.TestCase):
    def facts(self, include_reported_q4=False, q4_value="40"):
        facts = [
            fact("Revenues", date(2024, 1, 1), date(2024, 9, 29), "60",
                 accession="q3", filed=date(2024, 11, 1), fy=2024, fp="Q3"),
            fact("Revenues", date(2024, 7, 1), date(2024, 9, 29), "20",
                 accession="q3", filed=date(2024, 11, 1), fy=2024, fp="Q3"),
            fact("Revenues", date(2024, 1, 1), date(2024, 12, 31), "100",
                 accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"),
        ]
        if include_reported_q4:
            facts.append(fact("Revenues", date(2024, 9, 30), date(2024, 12, 31), q4_value,
                              accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"))
        return facts

    def test_q4_is_annual_minus_nine_months(self):
        view = build_quarterly_view(self.facts(), [], split_events=(), as_of=datetime(2025, 3, 1, tzinfo=UTC))
        q4 = view.effective("REVENUE")[QuarterKey(2024, 4)]
        self.assertEqual(q4.value, Decimal("40"))
        self.assertEqual(q4.kind, "DERIVED")
        self.assertEqual(q4.source, "SEC")
        self.assertEqual((q4.period_start, q4.period_end), (date(2024, 9, 30), date(2024, 12, 31)))
        self.assertEqual(len(q4.lineage), 2)

    def test_concept_outranks_reported_versus_derived(self):
        # PLD Q4 2025: a reported ProfitLoss (includes minority interest) must not
        # displace NetIncomeLoss derived exactly from official FY and 9M figures.
        facts = [
            fact("NetIncomeLoss", date(2024, 1, 1), date(2024, 9, 29), "60",
                 accession="q3", filed=date(2024, 11, 1), fy=2024, fp="Q3"),
            fact("NetIncomeLoss", date(2024, 7, 1), date(2024, 9, 29), "20",
                 accession="q3", filed=date(2024, 11, 1), fy=2024, fp="Q3"),
            fact("NetIncomeLoss", date(2024, 1, 1), date(2024, 12, 31), "100",
                 accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"),
            fact("ProfitLoss", date(2024, 9, 30), date(2024, 12, 31), "43",
                 accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"),
        ]
        view = build_quarterly_view(facts, [], split_events=(), as_of=datetime(2025, 3, 1, tzinfo=UTC))
        q4 = view.effective("NET_INCOME")[QuarterKey(2024, 4)]
        self.assertEqual((q4.concept, q4.kind, q4.value), ("NetIncomeLoss", "DERIVED", Decimal("40")))

    def test_reported_q4_wins_and_mismatch_is_recorded(self):
        view = build_quarterly_view(self.facts(True, "41"), [], split_events=(), as_of=datetime(2025, 3, 1, tzinfo=UTC))
        q4 = view.effective("REVENUE")[QuarterKey(2024, 4)]
        self.assertEqual((q4.kind, q4.value), ("REPORTED", Decimal("41")))
        self.assertIn("Q4_DERIVATION_MISMATCH", view.diagnostics)

    def test_eps_q4_is_never_derived(self):
        facts = [
            fact("EarningsPerShareDiluted", date(2024, 1, 1), date(2024, 9, 29), "3",
                 accession="q3", filed=date(2024, 11, 1), fy=2024, fp="Q3"),
            fact("EarningsPerShareDiluted", date(2024, 1, 1), date(2024, 12, 31), "4",
                 accession="k", filed=date(2025, 2, 1), fy=2024, fp="FY", form="10-K"),
        ]
        view = build_quarterly_view(facts, [], split_events=(), as_of=datetime(2025, 3, 1, tzinfo=UTC))
        self.assertNotIn(QuarterKey(2024, 4), view.effective("EPS_DILUTED"))

    def test_derivation_needs_both_inputs_visible(self):
        # The 10-K is filed 2025-02-01. Callers pass facts with observed_at <= as_of;
        # independently, nothing filed after as_of is ever visible.
        view = build_quarterly_view(self.facts(), [], split_events=(), as_of=datetime(2025, 1, 15, tzinfo=UTC))
        self.assertNotIn(QuarterKey(2024, 4), view.effective("REVENUE"))


class GrowthTests(unittest.TestCase):
    def test_latest_yoy_describes_latest_quarter_or_is_none(self):
        facts = calendar_year_company({(2024, 1): "1.00", (2024, 2): "1.00", (2025, 1): "1.10"})
        yahoo_rows = [yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2025, 6, 30), "2.00")]
        view = build_quarterly_view(facts, yahoo_rows, split_events=(), as_of=datetime(2025, 8, 1, tzinfo=UTC))
        latest = view.latest_growth("EPS_DILUTED")
        self.assertEqual(latest.key, QuarterKey(2025, 2))
        self.assertIsNone(latest.yoy_pct)
        self.assertIn("LATEST_QUARTER_YOY_UNAVAILABLE", latest.reasons)

    def test_yoy_never_crosses_sources(self):
        facts = calendar_year_company({(2024, 2): "1.00"})
        yahoo_rows = [yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2025, 6, 30), "2.00")]
        view = build_quarterly_view(facts, yahoo_rows, split_events=(), as_of=datetime(2025, 8, 1, tzinfo=UTC))
        self.assertIsNone(view.growth("EPS_DILUTED", QuarterKey(2025, 2)).yoy_pct)

    def test_yahoo_pair_gives_yoy_when_both_quarters_are_yahoo(self):
        facts = calendar_year_company({(2024, 1): "1.00", (2025, 1): "1.00"})
        yahoo_rows = [
            yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2024, 12, 31), "1.00"),
            yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2025, 12, 31), "1.50"),
        ]
        # Calendar: Q4 2024/2025 inferred from the SEC pattern of the annual interval.
        facts += [
            fact("EarningsPerShareDiluted", date(2024, 1, 1), date(2024, 12, 31), "4",
                 accession="k24", filed=date(2025, 2, 20), fy=2024, fp="FY", form="10-K"),
            fact("EarningsPerShareDiluted", date(2025, 1, 1), date(2025, 12, 31), "5",
                 accession="k25", filed=date(2026, 2, 20), fy=2025, fp="FY", form="10-K"),
        ]
        view = build_quarterly_view(facts, yahoo_rows, split_events=(), as_of=datetime(2026, 3, 1, tzinfo=UTC))
        growth = view.growth("EPS_DILUTED", QuarterKey(2025, 4))
        self.assertEqual(growth.source, "YAHOO")
        self.assertEqual(growth.yoy_pct, Decimal("50"))

    def test_yoy_uses_one_tag_and_falls_back_to_a_tag_present_in_both(self):
        facts = [
            fact("Revenues", date(2025, 1, 1), date(2025, 3, 31), "125",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
            fact("RevenueFromContractWithCustomerExcludingAssessedTax", date(2025, 1, 1), date(2025, 3, 31), "120",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
            fact("RevenueFromContractWithCustomerExcludingAssessedTax", date(2024, 1, 1), date(2024, 3, 31), "100",
                 accession="a", filed=date(2024, 5, 1), fy=2024, fp="Q1"),
        ]
        view = build_quarterly_view(facts, [], split_events=(), as_of=datetime(2025, 6, 1, tzinfo=UTC))
        growth = view.growth("REVENUE", QuarterKey(2025, 1))
        self.assertEqual(growth.concept, "RevenueFromContractWithCustomerExcludingAssessedTax")
        self.assertEqual(growth.yoy_pct, Decimal("20"))

    def test_total_revenue_wins_over_contract_revenue_subset(self):
        facts = [
            fact("Revenues", date(2025, 1, 1), date(2025, 3, 31), "101",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
            fact("RevenueFromContractWithCustomerExcludingAssessedTax", date(2025, 1, 1), date(2025, 3, 31), "70",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
        ]
        view = build_quarterly_view(facts, [], split_events=(), as_of=datetime(2025, 6, 1, tzinfo=UTC))
        self.assertEqual(view.effective("REVENUE")[QuarterKey(2025, 1)].concept, "Revenues")

    def test_bank_net_revenue_is_a_revenue_concept(self):
        facts = [
            fact("RevenuesNetOfInterestExpense", date(2024, 1, 1), date(2024, 3, 31), "50",
                 accession="a", filed=date(2024, 5, 1), fy=2024, fp="Q1"),
            fact("RevenuesNetOfInterestExpense", date(2025, 1, 1), date(2025, 3, 31), "60",
                 accession="b", filed=date(2025, 5, 1), fy=2025, fp="Q1"),
        ]
        view = build_quarterly_view(facts, [], split_events=(), as_of=datetime(2025, 6, 1, tzinfo=UTC))
        self.assertEqual(view.growth("REVENUE", QuarterKey(2025, 1)).yoy_pct, Decimal("20"))

    def test_non_positive_prior_gives_none_and_loss_to_profit(self):
        facts = calendar_year_company({(2024, 1): "-0.50", (2025, 1): "0.40"})
        view = build_quarterly_view(facts, [], split_events=(), as_of=datetime(2025, 6, 1, tzinfo=UTC))
        growth = view.growth("EPS_DILUTED", QuarterKey(2025, 1))
        self.assertIsNone(growth.yoy_pct)
        self.assertTrue(growth.loss_to_profit)


class SplitBasisTests(unittest.TestCase):
    events = (SplitEvent(date(2024, 6, 10), Decimal("10"), ("SEC", "YAHOO")),)

    def test_restated_and_original_values_converge_on_one_basis(self):
        facts = [
            fact("EarningsPerShareDiluted", date(2023, 5, 1), date(2023, 7, 30), "2.48",
                 accession="q23", filed=date(2023, 8, 28), fy=2024, fp="Q2"),
            fact("EarningsPerShareDiluted", date(2023, 5, 1), date(2023, 7, 30), "0.25",
                 accession="q24", filed=date(2024, 8, 28), fy=2025, fp="Q2"),
            fact("EarningsPerShareDiluted", date(2024, 4, 29), date(2024, 7, 28), "0.67",
                 accession="q24", filed=date(2024, 8, 28), fy=2025, fp="Q2"),
            fact("EarningsPerShareDiluted", date(2022, 5, 2), date(2022, 7, 31), "0.26",
                 accession="q22", filed=date(2022, 8, 31), fy=2023, fp="Q2"),
            fact("EarningsPerShareDiluted", date(2022, 5, 2), date(2022, 7, 31), "0.26",
                 accession="q23", filed=date(2023, 8, 28), fy=2024, fp="Q2"),
        ]
        view = build_quarterly_view(facts, [], split_events=self.events, as_of=datetime(2024, 10, 1, tzinfo=UTC))
        growth = view.growth("EPS_DILUTED", QuarterKey(2024, 2))
        self.assertAlmostEqual(float(growth.yoy_pct), 861.538, places=2)  # 0.25 / 0.026
        self.assertEqual(view.split_status_reasons, ())

    def test_unexplained_split_sized_restatement_fails_closed(self):
        facts = [
            fact("EarningsPerShareDiluted", date(2023, 5, 1), date(2023, 7, 30), "2.48",
                 accession="q23", filed=date(2023, 8, 28), fy=2024, fp="Q2"),
            fact("EarningsPerShareDiluted", date(2023, 5, 1), date(2023, 7, 30), "0.25",
                 accession="q24", filed=date(2024, 8, 28), fy=2025, fp="Q2"),
            fact("EarningsPerShareDiluted", date(2024, 4, 29), date(2024, 7, 28), "0.67",
                 accession="q24", filed=date(2024, 8, 28), fy=2025, fp="Q2"),
        ]
        view = build_quarterly_view(facts, [], split_events=(), as_of=datetime(2024, 10, 1, tzinfo=UTC))
        self.assertIn("UNDECLARED_SPLIT_SUSPECTED", view.split_status_reasons)

    def shares_pair(self, before, after):
        return [
            fact("WeightedAverageNumberOfDilutedSharesOutstanding", date(2021, 7, 1), date(2021, 9, 30), before,
                 accession="q21", filed=date(2021, 10, 30), fy=2021, fp="Q3"),
            fact("WeightedAverageNumberOfDilutedSharesOutstanding", date(2022, 7, 1), date(2022, 9, 30), "1000000",
                 accession="q22", filed=date(2022, 10, 30), fy=2022, fp="Q3"),
            fact("WeightedAverageNumberOfDilutedSharesOutstanding", date(2021, 7, 1), date(2021, 9, 30), after,
                 accession="q22", filed=date(2022, 10, 30), fy=2022, fp="Q3"),
            fact("EarningsPerShareDiluted", date(2021, 7, 1), date(2021, 9, 30), "1.00",
                 accession="q21", filed=date(2021, 10, 30), fy=2021, fp="Q3"),
        ]

    def test_provider_adjustment_contradicted_by_sec_shares_is_rejected(self):
        # Realty Income: yfinance lists a 1.032 "split" (Orion spin-off price adjustment);
        # SEC share counts do not change, so it is not a share split.
        provider_only = (SplitEvent(date(2021, 11, 15), Decimal("1.032"), ("YAHOO",)),)
        view = build_quarterly_view(self.shares_pair("1000000", "1000000"), [], split_events=provider_only,
                                    as_of=datetime(2023, 1, 1, tzinfo=UTC))
        self.assertEqual(view.rejected_split_events, provider_only)
        self.assertEqual(view.effective("EPS_DILUTED")[QuarterKey(2021, 3)].value, Decimal("1.00"))
        self.assertEqual(view.split_status_reasons, ())

    def test_provider_split_confirmed_by_sec_shares_is_applied(self):
        provider_only = (SplitEvent(date(2022, 6, 1), Decimal("2"), ("YAHOO",)),)
        view = build_quarterly_view(self.shares_pair("1000000", "2000000"), [], split_events=provider_only,
                                    as_of=datetime(2023, 1, 1, tzinfo=UTC))
        self.assertEqual(view.rejected_split_events, ())
        self.assertEqual(view.effective("EPS_DILUTED")[QuarterKey(2021, 3)].value, Decimal("0.50"))

    def test_filing_close_to_split_without_reference_is_excluded(self):
        facts = [fact("EarningsPerShareDiluted", date(2024, 2, 1), date(2024, 4, 28), "6.00",
                      accession="x", filed=date(2024, 6, 12), fy=2025, fp="Q1")]
        view = build_quarterly_view(facts, [], split_events=self.events, as_of=datetime(2024, 10, 1, tzinfo=UTC))
        self.assertNotIn(QuarterKey(2025, 1), view.effective("EPS_DILUTED"))
        self.assertIn("SPLIT_BASIS_UNCERTAIN", view.diagnostics)


class YahooSnapshotTests(unittest.TestCase):
    def test_latest_run_per_variant_wins(self):
        old = datetime(2026, 1, 1, tzinfo=UTC)
        new = datetime(2026, 2, 1, tzinfo=UTC)
        rows = [
            {**yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2025, 12, 31), "1.00", raw_id=1), "run_observed_at": old},
            {**yahoo("EPS_DILUTED", "quarterlyDilutedEPS", date(2025, 12, 31), "1.10", raw_id=2), "run_observed_at": new},
        ]
        snapshot = yahoo_rows_from_snapshot(rows, as_of=datetime(2026, 3, 1, tzinfo=UTC))
        self.assertEqual([row["id"] for row in snapshot], [2])
        snapshot = yahoo_rows_from_snapshot(rows, as_of=datetime(2026, 1, 15, tzinfo=UTC))
        self.assertEqual([row["id"] for row in snapshot], [1])


class RealDataTests(unittest.TestCase):
    NVDA_EVENTS = (
        SplitEvent(date(2021, 7, 20), Decimal("4"), ("SEC", "YAHOO")),
        SplitEvent(date(2024, 6, 10), Decimal("10"), ("SEC", "YAHOO")),
    )

    def test_nvda_split_affected_quarters_are_corrected(self):
        view = build_quarterly_view(real_facts("nvda"), [], split_events=self.NVDA_EVENTS,
                                    as_of=datetime(2026, 10, 2, tzinfo=UTC))
        expected = {  # audit: true growth on a consistent basis
            QuarterKey(2024, 2): 854,   # 2023-07-30: 2.48 vs 0.26 pre-split
            QuarterKey(2024, 3): 1274,  # 2023-10-29: 3.71 vs 0.27
            QuarterKey(2025, 1): 629,   # 2024-04-28: 5.98 vs 0.82
            QuarterKey(2022, 1): 106,   # 2021-05-02: 3.03 vs 1.47 (4:1)
        }
        for key, pct in expected.items():
            with self.subTest(key=key):
                growth = view.growth("EPS_DILUTED", key)
                self.assertIsNotNone(growth.yoy_pct)
                self.assertLess(abs(float(growth.yoy_pct) - pct) / pct, 0.03)
        self.assertNotIn("UNDECLARED_SPLIT_SUSPECTED", view.split_status_reasons)

    def test_real_histories_with_their_splits_raise_no_split_reasons(self):
        cases = {
            "nvda": self.NVDA_EVENTS,
            "aapl": (SplitEvent(date(2020, 8, 31), Decimal("4"), ("SEC", "YAHOO")),),
        }
        for ticker, events in cases.items():
            with self.subTest(ticker=ticker):
                view = build_quarterly_view(real_facts(ticker), [], split_events=events,
                                            as_of=datetime(2026, 10, 2, tzinfo=UTC))
                self.assertEqual(view.split_status_reasons, ())
                self.assertEqual(view.diagnostics, ())

    def test_old_restatement_outside_window_is_not_a_split(self):
        # AAPL restated FY2009 EPS 1.35 -> 2.01 (revenue-recognition change), a 1.49
        # ratio. It predates the six-year window and must not look like a 3:2 split.
        view = build_quarterly_view(real_facts("aapl"), [], split_events=(),
                                    as_of=datetime(2026, 9, 3, tzinfo=UTC))
        self.assertEqual(view.split_status_reasons, ())

    def test_nvda_without_split_events_detects_undeclared_split(self):
        view = build_quarterly_view(real_facts("nvda"), [], split_events=(),
                                    as_of=datetime(2026, 10, 2, tzinfo=UTC))
        self.assertIn("UNDECLARED_SPLIT_SUSPECTED", view.split_status_reasons)

    def test_aapl_q4_revenue_and_net_income_are_derived_exactly(self):
        view = build_quarterly_view(real_facts("aapl"), [], split_events=(),
                                    as_of=datetime(2026, 10, 2, tzinfo=UTC))
        q4 = view.effective("REVENUE")[QuarterKey(2025, 4)]
        self.assertEqual(q4.kind, "DERIVED")
        self.assertEqual(q4.period_end, date(2025, 9, 27))
        self.assertEqual(q4.value, Decimal("102466000000"))  # Yahoo-reported FY25 Q4 revenue
        self.assertEqual(view.effective("NET_INCOME")[QuarterKey(2025, 4)].value, Decimal("27466000000"))
        self.assertNotIn(QuarterKey(2025, 4), view.effective("EPS_DILUTED"))

    def test_aapl_baseline_growth_matches_legacy_where_legacy_was_right(self):
        view = build_quarterly_view(real_facts("aapl"), [], split_events=(),
                                    as_of=datetime(2026, 9, 3, tzinfo=UTC))
        latest = view.latest_growth("EPS_DILUTED")
        self.assertEqual(latest.key, QuarterKey(2026, 3))
        self.assertAlmostEqual(float(latest.yoy_pct), 28.662420382, places=6)
        self.assertAlmostEqual(float(view.latest_growth("REVENUE").yoy_pct), 16.356501765, places=6)


if __name__ == "__main__":
    unittest.main()
