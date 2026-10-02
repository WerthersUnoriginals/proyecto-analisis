"""Offline tests for annual normalization sec-annual-v1 (A on fundamentals v3)."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.annual_v3 import build_annual_view
from database.split_basis import SplitEvent
from test_quarterly_v3 import fact, real_facts

UTC = timezone.utc
NVDA_EVENTS = (
    SplitEvent(date(2021, 7, 20), Decimal("4"), ("SEC", "YAHOO")),
    SplitEvent(date(2024, 6, 10), Decimal("10"), ("SEC", "YAHOO")),
)


def annual_company(eps, *, revenue=None, net_income=None, equity=None, tag="EarningsPerShareDiluted"):
    """Calendar-year company with one 10-K per year filed the following February."""
    facts = []
    for year, value in eps.items():
        start, end, filed = date(year, 1, 1), date(year, 12, 31), date(year + 1, 2, 20)
        common = dict(accession=f"k{year}", filed=filed, fy=year, fp="FY", form="10-K")
        facts.append(fact(tag, start, end, value, **common))
        if revenue:
            facts.append(fact("Revenues", start, end, revenue[year], **common))
        if net_income:
            facts.append(fact("NetIncomeLoss", start, end, net_income[year], **common))
        if equity:
            facts.append(fact("StockholdersEquity", None, end, equity[year], **common, unit="USD"))
            if year - 1 in equity and year - 1 not in eps:
                facts.append(fact("StockholdersEquity", None, date(year - 1, 12, 31), equity[year - 1],
                                  **common, unit="USD"))
    return facts


AS_OF = datetime(2026, 3, 15, tzinfo=UTC)


class GrowthTests(unittest.TestCase):
    def test_yoy_and_statuses(self):
        view = build_annual_view(annual_company({2022: "1", 2023: "-0.5", 2024: "0.5", 2025: "1"}),
                                 split_events=(), as_of=AS_OF)
        self.assertEqual(view.latest_year("EPS_DILUTED"), 2025)
        self.assertEqual(view.growth("EPS_DILUTED", 2025).yoy_pct, Decimal("100"))
        self.assertEqual(view.growth("EPS_DILUTED", 2024).status, "LOSS_TO_PROFIT")
        fall_into_loss = view.growth("EPS_DILUTED", 2023)  # 1 -> -0.5 from a positive base
        self.assertEqual((fall_into_loss.status, fall_into_loss.yoy_pct), ("GROWTH", Decimal("-150")))
        self.assertEqual(view.growth("EPS_DILUTED", 2022).status, "NO_DATA")

    def test_cagr_anchored_at_latest_year(self):
        view = build_annual_view(annual_company({2021: "1", 2022: "1.25", 2023: "1.5", 2024: "1.75", 2025: "2"}),
                                 split_events=(), as_of=AS_OF)
        cagr = view.cagr("EPS_DILUTED", 3)
        self.assertAlmostEqual(float(cagr.value_pct), (2 / 1.25) ** (1 / 3) * 100 - 100, places=9)
        self.assertEqual((cagr.start.fiscal_year, cagr.end.fiscal_year), (2022, 2025))

    def test_cagr_never_uses_an_older_window(self):
        view = build_annual_view(annual_company({2019: "1", 2020: "1", 2021: "1", 2022: "1", 2024: "2", 2025: "2"}),
                                 split_events=(), as_of=AS_OF)
        cagr = view.cagr("EPS_DILUTED", 3)
        self.assertIsNone(cagr.value_pct)
        self.assertIn("INSUFFICIENT_HISTORY", cagr.reasons)

    def test_cagr_with_non_positive_endpoint(self):
        view = build_annual_view(annual_company({2022: "-1", 2023: "1", 2024: "1", 2025: "2"}),
                                 split_events=(), as_of=AS_OF)
        self.assertIn("NON_POSITIVE_ENDPOINT", view.cagr("EPS_DILUTED", 3).reasons)

    def test_cagr_never_crosses_tags(self):
        facts = annual_company({2022: "1", 2023: "1"}, tag="EarningsPerShareBasicAndDiluted")
        facts += annual_company({2024: "2", 2025: "2"})
        cagr = build_annual_view(facts, split_events=(), as_of=AS_OF).cagr("EPS_DILUTED", 3)
        self.assertIsNone(cagr.value_pct)
        self.assertIn("NO_COMMON_CONCEPT", cagr.reasons)

    def test_unfinished_year_is_not_annual(self):
        facts = annual_company({2024: "1", 2025: "2"})
        facts.append(fact("EarningsPerShareDiluted", date(2026, 1, 1), date(2026, 9, 30), "9",
                          accession="q", filed=date(2026, 11, 1), fy=2026, fp="Q3"))
        view = build_annual_view(facts, split_events=(), as_of=datetime(2026, 12, 1, tzinfo=UTC))
        self.assertEqual(view.latest_year("EPS_DILUTED"), 2025)


class RoeTests(unittest.TestCase):
    def test_roe_uses_average_equity(self):
        facts = annual_company({2024: "1", 2025: "1"}, net_income={2024: "10", 2025: "30"},
                               equity={2023: "80", 2024: "100", 2025: "200"})
        roe = build_annual_view(facts, split_events=(), as_of=AS_OF).roe(2025)
        self.assertEqual(roe.status, "OK")
        self.assertEqual(roe.value_pct, Decimal("20"))  # 30 / ((100 + 200) / 2)

    def test_non_positive_equity_is_not_meaningful(self):
        facts = annual_company({2024: "1", 2025: "1"}, net_income={2024: "10", 2025: "30"},
                               equity={2024: "-50", 2025: "20"})
        roe = build_annual_view(facts, split_events=(), as_of=AS_OF).roe(2025)
        self.assertEqual(roe.status, "NOT_MEANINGFUL")
        self.assertIsNone(roe.value_pct)

    def test_missing_opening_equity_is_no_data(self):
        facts = annual_company({2025: "1"}, net_income={2025: "30"}, equity={2025: "200"})
        self.assertEqual(build_annual_view(facts, split_events=(), as_of=AS_OF).roe(2025).status, "NO_DATA")


class RealDataTests(unittest.TestCase):
    def test_nvda_annual_eps_is_split_consistent(self):
        view = build_annual_view(real_facts("nvda"), split_events=NVDA_EVENTS, as_of=datetime(2026, 10, 2, tzinfo=UTC))
        self.assertEqual(view.latest_year("EPS_DILUTED"), 2026)
        fy2023 = view.growth("EPS_DILUTED", 2023)
        self.assertAlmostEqual(float(fy2023.yoy_pct), -55.8, delta=0.5)  # not -95.6
        fy2022 = view.growth("EPS_DILUTED", 2022)
        self.assertAlmostEqual(float(fy2022.yoy_pct), 122.5, delta=0.5)
        self.assertEqual(view.split_status_reasons, ())

    def test_nvda_without_events_flags_the_mixed_bases(self):
        view = build_annual_view(real_facts("nvda"), split_events=(), as_of=datetime(2026, 10, 2, tzinfo=UTC))
        self.assertIn("UNDECLARED_SPLIT_SUSPECTED", view.split_status_reasons)

    def test_aapl_roe_is_computed_and_large(self):
        view = build_annual_view(real_facts("aapl"), split_events=(), as_of=datetime(2026, 10, 2, tzinfo=UTC))
        self.assertEqual(view.latest_year("EPS_DILUTED"), 2025)
        roe = view.roe(2025)
        self.assertEqual(roe.status, "OK")
        self.assertGreater(roe.value_pct, 100)  # buybacks shrink equity
        self.assertEqual(roe.concepts, ("NetIncomeLoss", "StockholdersEquity"))


if __name__ == "__main__":
    unittest.main()
