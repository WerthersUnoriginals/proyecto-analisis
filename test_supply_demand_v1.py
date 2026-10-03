"""Offline tests for the S calculations (s-supply-demand-v1)."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.annual_v3 import ANNUAL_METRICS, build_annual_view
from database.sec_facts import CATALOG_VERSION, SEC_TAG_CATALOG
from database.split_basis import SplitEvent
from database.supply_demand_v1 import (
    DEBT_DEFINITIONS,
    S_ANNUAL_METRICS,
    S_INSTANT_METRICS,
    SUPPLY_DEMAND_VERSION,
    compute_leverage,
    compute_share_supply,
    compute_volume_demand,
)
from test_annual_v3 import annual_company
from test_new_highs_v1 import series
from test_quarterly_v3 import fact

UTC = timezone.utc
AS_OF = datetime(2026, 3, 15, tzinfo=UTC)


def shares_company(shares, *, debt=None, equity=None, debt_tags=("LongTermDebt",)):
    """Calendar-year company: diluted shares, debt and equity per fiscal year."""

    facts = []
    for year, value in shares.items():
        start, end, filed = date(year, 1, 1), date(year, 12, 31), date(year + 1, 2, 20)
        common = dict(accession=f"k{year}", filed=filed, fy=year, fp="FY", form="10-K")
        facts.append(fact("WeightedAverageNumberOfDilutedSharesOutstanding", start, end, value, **common))
        facts.append(fact("EarningsPerShareDiluted", start, end, "1", **common))
        if equity and year in equity:
            facts.append(fact("StockholdersEquity", None, end, equity[year], **common, unit="USD"))
        if debt and year in debt:
            parts = debt[year] if isinstance(debt[year], tuple) else (debt[year],)
            for tag, part in zip(debt_tags, parts):
                facts.append(fact(tag, None, end, part, **common, unit="USD"))
    return facts


def view(facts, events=()):
    return build_annual_view(facts, split_events=events, as_of=AS_OF,
                             metrics=S_ANNUAL_METRICS, instant_metrics=S_INSTANT_METRICS)


class CatalogAndViewTests(unittest.TestCase):
    def test_catalog_v6_adds_debt(self):
        self.assertEqual(CATALOG_VERSION, "sec-tag-catalog-v7")
        self.assertEqual(SEC_TAG_CATALOG["DEBT"][0], "LongTermDebt")
        self.assertEqual(SUPPLY_DEMAND_VERSION, "s-supply-demand-v1")

    def test_annual_view_defaults_are_unchanged_for_a(self):
        self.assertEqual(ANNUAL_METRICS, ("EPS_DILUTED", "REVENUE", "NET_INCOME"))
        default = build_annual_view(shares_company({2024: "100", 2025: "95"}), split_events=(), as_of=AS_OF)
        self.assertIsNone(default.latest_year("DILUTED_SHARES"))
        self.assertEqual(view(shares_company({2024: "100", 2025: "95"})).latest_year("DILUTED_SHARES"), 2025)


class ShareSupplyTests(unittest.TestCase):
    def test_buyback_is_negative_change(self):
        result = compute_share_supply(view(shares_company({2021: "110", 2022: "100", 2023: "98", 2024: "96", 2025: "90"})))
        self.assertEqual(result.latest_fiscal_year, 2025)
        self.assertEqual(result.diluted_shares_latest, Decimal("90"))
        self.assertEqual(result.change_1y_pct, Decimal("-6.25"))
        self.assertEqual(result.change_3y_pct, Decimal("-10"))

    def test_three_year_change_needs_the_anchored_window(self):
        result = compute_share_supply(view(shares_company({2023: "98", 2024: "96", 2025: "90"})))
        self.assertIsNone(result.change_3y_pct)
        self.assertIn("INSUFFICIENT_HISTORY", result.reasons_3y)

    def test_split_is_converted_to_the_as_of_basis(self):
        # 4:1 split in mid-2024: the 2023 10-K (filed before) reports pre-split shares.
        events = (SplitEvent(date(2024, 6, 1), Decimal("4"), ("SEC", "YAHOO")),)
        facts = shares_company({2022: "100", 2023: "100"}) + shares_company({2024: "400", 2025: "400"})
        result = compute_share_supply(view(facts, events))
        self.assertEqual(result.change_1y_pct, Decimal("0"))

    def test_basic_shares_when_diluted_stopped(self):
        # XOM: diluted shares last tagged long ago, basic shares every year.
        facts = shares_company({2012: "50"})
        for year, value in {2021: "110", 2022: "105", 2023: "100", 2024: "98", 2025: "99"}.items():
            facts.append(fact("WeightedAverageNumberOfSharesOutstandingBasic", date(year, 1, 1), date(year, 12, 31),
                              value, accession=f"b{year}", filed=date(year + 1, 2, 20), fy=year, fp="FY", form="10-K"))
            facts.append(fact("EarningsPerShareDiluted", date(year, 1, 1), date(year, 12, 31), "1",
                              accession=f"b{year}", filed=date(year + 1, 2, 20), fy=year, fp="FY", form="10-K"))
        result = compute_share_supply(view(facts))
        self.assertEqual(result.concept, "WeightedAverageNumberOfSharesOutstandingBasic")
        self.assertEqual(result.latest_fiscal_year, 2025)
        self.assertAlmostEqual(float(result.change_3y_pct), (99 / 105 - 1) * 100, places=9)

    def test_diluted_preferred_when_equally_recent(self):
        facts = shares_company({2024: "100", 2025: "95"})
        for year in (2024, 2025):
            facts.append(fact("WeightedAverageNumberOfSharesOutstandingBasic", date(year, 1, 1), date(year, 12, 31),
                              "90", accession=f"k{year}", filed=date(year + 1, 2, 20), fy=year, fp="FY", form="10-K"))
        self.assertEqual(compute_share_supply(view(facts)).concept, "WeightedAverageNumberOfDilutedSharesOutstanding")

    def test_no_shares(self):
        result = compute_share_supply(view([]))
        self.assertIsNone(result.latest_fiscal_year)
        self.assertEqual(result.reasons_1y, ("NO_VALUE",))


class LeverageTests(unittest.TestCase):
    def test_debt_to_equity_and_change_with_long_term_debt(self):
        facts = shares_company({2021: "1", 2022: "1", 2023: "1", 2024: "1", 2025: "1"},
                               debt={2022: "60", 2025: "30"}, equity={2022: "100", 2025: "100"})
        result = compute_leverage(view(facts))
        self.assertEqual(result.fiscal_year, 2025)
        self.assertEqual(result.definition, "LongTermDebt")
        self.assertEqual(result.debt_to_equity_latest, Decimal("0.3"))
        self.assertEqual(result.debt_to_equity_3y_ago, Decimal("0.6"))
        self.assertEqual(result.change_3y, Decimal("-0.3"))

    def test_noncurrent_plus_current_definition(self):
        facts = shares_company({2025: "1"}, debt={2025: ("40", "10")}, equity={2025: "100"},
                               debt_tags=("LongTermDebtNoncurrent", "LongTermDebtCurrent"))
        result = compute_leverage(view(facts))
        self.assertEqual(result.definition, "LongTermDebtNoncurrent+LongTermDebtCurrent")
        self.assertEqual(result.debt_to_equity_latest, Decimal("0.5"))

    def test_noncurrent_alone_is_not_used(self):
        facts = shares_company({2025: "1"}, debt={2025: "40"}, equity={2025: "100"},
                               debt_tags=("LongTermDebtNoncurrent",))
        result = compute_leverage(view(facts))
        self.assertIsNone(result.debt_to_equity_latest)
        self.assertIn("NO_DEBT_EVIDENCE", result.reasons)

    def test_definitions_are_never_mixed_across_years(self):
        facts = shares_company({2022: "1"}, debt={2022: ("40", "10")}, equity={2022: "100"},
                               debt_tags=("LongTermDebtNoncurrent", "LongTermDebtCurrent"))
        facts += shares_company({2025: "1"}, debt={2025: "30"}, equity={2025: "100"})
        result = compute_leverage(view(facts))
        self.assertEqual(result.debt_to_equity_latest, Decimal("0.3"))
        self.assertIsNone(result.change_3y)
        self.assertIn("NO_COMPARABLE_DEBT_3Y", result.reasons)

    def test_absent_debt_is_not_zero(self):
        result = compute_leverage(view(shares_company({2025: "1"}, equity={2025: "100"})))
        self.assertIsNone(result.debt_to_equity_latest)
        self.assertEqual(result.status, "NO_DEBT_EVIDENCE")

    def test_reported_zero_debt_is_zero(self):
        result = compute_leverage(view(shares_company({2025: "1"}, debt={2025: "0"}, equity={2025: "100"})))
        self.assertEqual(result.debt_to_equity_latest, Decimal("0"))

    def test_non_positive_equity_is_not_meaningful(self):
        result = compute_leverage(view(shares_company({2025: "1"}, debt={2025: "50"}, equity={2025: "-10"})))
        self.assertEqual(result.status, "NOT_MEANINGFUL")
        self.assertIsNone(result.debt_to_equity_latest)

    def test_definition_order(self):
        self.assertEqual(DEBT_DEFINITIONS[0], ("LongTermDebt",))


class VolumeDemandTests(unittest.TestCase):
    def bars(self, closes, volumes):
        bars = series(closes)
        return [bar.__class__(**{**bar.__dict__, "volume": Decimal(volume)}) for bar, volume in zip(bars, volumes)]

    def test_up_down_ratio_over_last_50_sessions(self):
        closes = [100] + [101, 100] * 25          # 51 bars: 25 up, 25 down sessions
        volumes = [999] + [300, 100] * 25
        result = compute_volume_demand(self.bars(closes, volumes), date(2026, 10, 2))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.up_down_volume_ratio_50d, Decimal(3))
        self.assertEqual(result.sessions, 50)

    def test_only_the_last_50_sessions_count(self):
        closes = [100, 50] + [101, 100] * 25
        volumes = [1, 10 ** 9] + [100, 100] * 25
        result = compute_volume_demand(self.bars(closes, volumes), date(2026, 10, 2))
        self.assertEqual(result.up_down_volume_ratio_50d, Decimal(1))

    def test_unchanged_sessions_are_ignored(self):
        closes = [100] + [100] * 10 + [101, 100] * 20
        volumes = [1] + [10 ** 6] * 10 + [200, 100] * 20
        result = compute_volume_demand(self.bars(closes, volumes), date(2026, 10, 2))
        self.assertEqual(result.up_down_volume_ratio_50d, Decimal(2))

    def test_insufficient_history(self):
        result = compute_volume_demand(self.bars([100] * 50, [1] * 50), date(2026, 10, 2))
        self.assertEqual(result.status, "INSUFFICIENT_HISTORY")
        self.assertIsNone(result.up_down_volume_ratio_50d)

    def test_no_down_volume(self):
        closes = list(range(100, 151))
        result = compute_volume_demand(self.bars(closes, [10] * 51), date(2026, 10, 2))
        self.assertIsNone(result.up_down_volume_ratio_50d)
        self.assertIn("NO_DOWN_VOLUME", result.reasons)

    def test_stale_prices(self):
        bars = series([100, 101] * 30, end=date(2026, 9, 18))
        result = compute_volume_demand(bars, date(2026, 10, 2))
        self.assertEqual(result.status, "STALE_PRICES")


if __name__ == "__main__":
    unittest.main()
