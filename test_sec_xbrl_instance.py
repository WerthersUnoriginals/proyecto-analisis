"""Offline tests for reading a filing's own XBRL instance (sec-xbrl-instance-v1)."""

import unittest
from datetime import date
from decimal import Decimal

from database.sec_xbrl_instance import INSTANCE_ORIGIN, choose_instance_file, parse_xbrl_instance

INSTANCE = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:us-gaap="http://fasb.org/us-gaap/2025"
            xmlns:dei="http://xbrl.sec.gov/dei/2025"
            xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
            xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
            xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <xbrli:context id="Q2">
    <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000753308</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="YTD">
    <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000753308</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="I">
    <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000753308</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="Q2_FPL">
    <xbrli:entity>
      <xbrli:identifier scheme="http://www.sec.gov/CIK">0000753308</xbrli:identifier>
      <xbrli:segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">nee:FPLMember</xbrldi:explicitMember></xbrli:segment>
    </xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="OTHER">
    <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000037634</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
  <xbrli:unit id="usdPerShare"><xbrli:divide>
    <xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
  </xbrli:divide></xbrli:unit>
  <dei:DocumentFiscalYearFocus contextRef="YTD">2026</dei:DocumentFiscalYearFocus>
  <dei:DocumentFiscalPeriodFocus contextRef="YTD">Q2</dei:DocumentFiscalPeriodFocus>
  <us-gaap:RegulatedAndUnregulatedOperatingRevenue contextRef="Q2" unitRef="usd" decimals="-6">7012000000</us-gaap:RegulatedAndUnregulatedOperatingRevenue>
  <us-gaap:RegulatedAndUnregulatedOperatingRevenue contextRef="Q2_FPL" unitRef="usd" decimals="-6">5000000000</us-gaap:RegulatedAndUnregulatedOperatingRevenue>
  <us-gaap:EarningsPerShareDiluted contextRef="Q2" unitRef="usdPerShare" decimals="2">1.50</us-gaap:EarningsPerShareDiluted>
  <us-gaap:EarningsPerShareDiluted contextRef="OTHER" unitRef="usdPerShare" decimals="2">9.99</us-gaap:EarningsPerShareDiluted>
  <us-gaap:NetIncomeLoss contextRef="YTD" unitRef="usd" decimals="-6">5000000000</us-gaap:NetIncomeLoss>
  <us-gaap:NetIncomeLoss contextRef="Q2" unitRef="usd" xsi:nil="true"/>
  <us-gaap:StockholdersEquity contextRef="I" unitRef="usd" decimals="-6">55000000000</us-gaap:StockholdersEquity>
  <us-gaap:Assets contextRef="I" unitRef="usd" decimals="-6">200000000000</us-gaap:Assets>
</xbrli:xbrl>
"""


def parse():
    return parse_xbrl_instance(INSTANCE, cik="0000753308", accession="0000753308-26-000060",
                               form="10-Q", filed_date=date(2026, 7, 24))


class ParseTests(unittest.TestCase):
    def test_only_catalog_facts_of_the_consolidated_registrant(self):
        facts = parse()
        by_tag = {(fact.tag, fact.period_start): fact for fact in facts}
        self.assertEqual(len(facts), 4)
        revenue = by_tag[("RegulatedAndUnregulatedOperatingRevenue", date(2026, 4, 1))]
        self.assertEqual(revenue.value, Decimal("7012000000"))  # not the FPL member
        self.assertEqual(by_tag[("EarningsPerShareDiluted", date(2026, 4, 1))].value, Decimal("1.50"))
        self.assertNotIn(("Assets", None), by_tag)

    def test_units_periods_and_filing_metadata(self):
        facts = {fact.tag: fact for fact in parse()}
        self.assertEqual(facts["EarningsPerShareDiluted"].unit, "USD/shares")
        self.assertEqual(facts["StockholdersEquity"].unit, "USD")
        self.assertIsNone(facts["StockholdersEquity"].period_start)
        self.assertEqual(facts["StockholdersEquity"].period_end, date(2026, 6, 30))
        eps = facts["EarningsPerShareDiluted"]
        self.assertEqual((eps.fiscal_year, eps.fiscal_period, eps.form), (2026, "Q2", "10-Q"))
        self.assertEqual((eps.accession, eps.filed_date), ("0000753308-26-000060", date(2026, 7, 24)))
        self.assertEqual(eps.origin, INSTANCE_ORIGIN)

    def test_nil_facts_are_skipped(self):
        net_income = [fact for fact in parse() if fact.tag == "NetIncomeLoss"]
        self.assertEqual([fact.period_start for fact in net_income], [date(2026, 1, 1)])

    def test_choose_instance_file(self):
        names = ["x-index.html", "nee-20260630.htm", "nee-20260630_htm.xml", "nee-20260630_lab.xml", "FilingSummary.xml"]
        self.assertEqual(choose_instance_file(names), "nee-20260630_htm.xml")
        legacy = ["aapl-20110625.xml", "aapl-20110625_cal.xml", "aapl-20110625.xsd", "FilingSummary.xml"]
        self.assertEqual(choose_instance_file(legacy), "aapl-20110625.xml")
        self.assertIsNone(choose_instance_file(["a.htm", "FilingSummary.xml"]))


if __name__ == "__main__":
    unittest.main()
