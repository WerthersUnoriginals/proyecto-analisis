"""Offline tests for verbatim SEC Company Facts extraction (v3)."""

import json
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from database.sec_facts import (
    CATALOG_VERSION,
    SEC_TAG_CATALOG,
    SecFact,
    duration_class,
    parse_companyfacts,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(ticker):
    path = FIXTURES / f"sec_companyfacts_{ticker}_2026-10-02.json"
    return json.loads(path.read_text(encoding="utf-8"))


def item(**overrides):
    base = {
        "start": "2024-01-01", "end": "2024-03-31", "val": 1.5,
        "accn": "0000000000-24-000001", "fy": 2024, "fp": "Q1",
        "form": "10-Q", "filed": "2024-05-01",
    }
    base.update(overrides)
    return base


def payload(tag="EarningsPerShareDiluted", unit="USD/shares", items=None):
    return {"cik": 1, "facts": {"us-gaap": {tag: {"units": {unit: items or [item()]}}}}}


class ParseTests(unittest.TestCase):
    def test_parses_verbatim_fields_with_decimal_values(self):
        facts = parse_companyfacts(payload())
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        self.assertIsInstance(fact, SecFact)
        self.assertEqual(fact.taxonomy, "us-gaap")
        self.assertEqual(fact.tag, "EarningsPerShareDiluted")
        self.assertEqual(fact.unit, "USD/shares")
        self.assertEqual(fact.period_start, date(2024, 1, 1))
        self.assertEqual(fact.period_end, date(2024, 3, 31))
        self.assertEqual(fact.value, Decimal("1.5"))
        self.assertEqual(fact.accession, "0000000000-24-000001")
        self.assertEqual((fact.fiscal_year, fact.fiscal_period), (2024, "Q1"))
        self.assertEqual((fact.form, fact.filed_date), ("10-Q", date(2024, 5, 1)))

    def test_ignores_tags_outside_catalog(self):
        self.assertEqual(parse_companyfacts(payload(tag="Assets", unit="USD")), [])

    def test_keeps_every_form_and_duration_verbatim(self):
        items = [
            item(),
            item(start="2023-04-01", end="2024-03-31", accn="a2", form="10-K", fp="FY"),
            item(start=None, end="2024-03-31", accn="a3", form="8-K"),
        ]
        items[2].pop("start")
        facts = parse_companyfacts(payload(items=items))
        self.assertEqual(len(facts), 3)
        by_accession = {fact.accession: fact for fact in facts}
        self.assertIsNone(by_accession["a3"].period_start)
        self.assertEqual(by_accession["a3"].form, "8-K")

    def test_integer_values_are_exact(self):
        facts = parse_companyfacts(payload(tag="NetIncomeLoss", unit="USD", items=[item(val=23434000000)]))
        self.assertEqual(facts[0].value, Decimal("23434000000"))

    def test_float_values_use_shortest_repr(self):
        facts = parse_companyfacts(payload(items=[item(val=0.1)]))
        self.assertEqual(facts[0].value, Decimal("0.1"))

    def test_identical_duplicate_items_collapse(self):
        facts = parse_companyfacts(payload(items=[item(), item(frame="CY2024Q1")]))
        self.assertEqual(len(facts), 1)

    def test_conflicting_duplicate_identity_raises(self):
        with self.assertRaises(ValueError):
            parse_companyfacts(payload(items=[item(), item(val=2.0)]))

    def test_nonfinite_or_missing_value_raises(self):
        with self.assertRaises(ValueError):
            parse_companyfacts(payload(items=[item(val=None)]))


class DurationTests(unittest.TestCase):
    def fact(self, start, end):
        return SecFact(
            taxonomy="us-gaap", tag="NetIncomeLoss", unit="USD",
            period_start=start, period_end=end, value=Decimal(1), accession="a",
            fiscal_year=2024, fiscal_period="Q1", form="10-Q",
            filed_date=date(2024, 5, 1), frame=None,
        )

    def test_classes(self):
        cases = [
            (date(2024, 1, 1), date(2024, 3, 31), "QUARTER"),
            (date(2024, 1, 1), date(2024, 6, 30), "YTD_6M"),
            (date(2024, 1, 1), date(2024, 9, 29), "YTD_9M"),
            (date(2024, 1, 1), date(2024, 12, 31), "ANNUAL"),
            (date(2024, 1, 1), date(2024, 1, 31), "OTHER"),
            (None, date(2024, 1, 31), "INSTANT"),
        ]
        for start, end, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(duration_class(self.fact(start, end)), expected)


class RealFixtureTests(unittest.TestCase):
    def test_nvda_fixture_parses_and_contains_split_ratio_facts(self):
        facts = parse_companyfacts(load_fixture("nvda"))
        ratios = {f.value for f in facts if f.tag == "StockholdersEquityNoteStockSplitConversionRatio1"}
        self.assertEqual(ratios, {Decimal(4), Decimal(10)})
        self.assertGreater(len(facts), 1000)

    def test_aapl_fixture_keeps_annual_and_ytd_facts(self):
        facts = parse_companyfacts(load_fixture("aapl"))
        classes = {duration_class(f) for f in facts if f.tag == "NetIncomeLoss"}
        self.assertTrue({"QUARTER", "YTD_9M", "ANNUAL"} <= classes)

    def test_catalog_is_versioned_and_ordered(self):
        self.assertEqual(CATALOG_VERSION, "sec-tag-catalog-v5")
        self.assertEqual(SEC_TAG_CATALOG["STOCKHOLDERS_EQUITY"][0], "StockholdersEquity")
        self.assertEqual(SEC_TAG_CATALOG["EPS_DILUTED"][0], "EarningsPerShareDiluted")
        self.assertEqual(
            SEC_TAG_CATALOG["REVENUE"][:4],
            # Totals first; ASC 606 contract revenue is a subset when both are reported
            # (BRK: Revenues 101.8B vs contract revenue 70.1B).
            ("Revenues", "RevenuesNetOfInterestExpense", "RegulatedAndUnregulatedOperatingRevenue",
             "RevenueFromContractWithCustomerExcludingAssessedTax"),
        )


if __name__ == "__main__":
    unittest.main()
