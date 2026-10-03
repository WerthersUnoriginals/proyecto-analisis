"""Opt-in real PostgreSQL validation of the v3 evidence repository.

Run with CANSLIM_RUN_PG_INTEGRATION=1. Every test works inside one transaction
that is rolled back, so the canslim database keeps no trace of the test.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from database import evidence_v3
from database.prices_v3 import PriceBar
from database.providers_v3 import FilingRecord
from database.registrant_v3 import RegistrantLink
from database.sec_facts import SecFact

RUN_FLAG = "CANSLIM_RUN_PG_INTEGRATION"
UTC = timezone.utc
COMPANY_ID = 1  # AAPL, present in every canslim installation of this project
CIK = "0000320193"
OBSERVED = datetime(2001, 1, 2, tzinfo=UTC)  # far before any real evidence


def fact(value="1.00", *, start=date(2000, 1, 1), end=date(2000, 3, 31), accession="TEST-0000000001"):
    return SecFact(
        taxonomy="us-gaap", tag="EarningsPerShareDiluted", unit="USD/shares",
        period_start=start, period_end=end, value=Decimal(value), accession=accession,
        fiscal_year=2000, fiscal_period="Q1", form="10-Q", filed_date=date(2000, 5, 1), frame=None,
    )


@unittest.skipUnless(os.getenv(RUN_FLAG) == "1", f"set {RUN_FLAG}=1 to run against PostgreSQL")
class EvidenceRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        from database.db import get_connection

        self.connection = get_connection()
        self.cursor = self.connection.cursor()
        self.cursor.execute("SELECT ticker, cik FROM public.companies WHERE id = %s;", (COMPANY_ID,))
        self.assertEqual(self.cursor.fetchone(), ("AAPL", CIK), "unexpected database identity")

    def tearDown(self):
        self.connection.rollback()
        self.connection.close()

    def run_id(self, status="SUCCESS", error_code=None):
        return evidence_v3.record_run(
            self.cursor, company_id=COMPANY_ID, provider="SEC", operation="test.operation",
            contract_version="test-v1", started_at=OBSERVED, completed_at=OBSERVED,
            status=status, error_code=error_code,
        )

    def expect_database_error(self, statement, params=()):
        import psycopg

        with self.assertRaises(psycopg.Error):
            with self.connection.transaction():
                self.cursor.execute(statement, params)

    def test_fact_retry_is_idempotent_and_changed_value_conflicts(self):
        run = self.run_id()
        self.assertEqual(
            evidence_v3.insert_sec_facts(self.cursor, company_id=COMPANY_ID, cik=CIK, facts=[fact()],
                                         observed_at=OBSERVED, run_id=run), (1, 0))
        self.assertEqual(
            evidence_v3.insert_sec_facts(self.cursor, company_id=COMPANY_ID, cik=CIK, facts=[fact()],
                                         observed_at=OBSERVED, run_id=run), (0, 1))
        with self.assertRaises(evidence_v3.EvidenceConflict):
            evidence_v3.insert_sec_facts(self.cursor, company_id=COMPANY_ID, cik=CIK, facts=[fact("2.00")],
                                         observed_at=OBSERVED, run_id=run)

    def test_instant_facts_deduplicate_with_null_start(self):
        run = self.run_id()
        instant = fact(start=None)
        evidence_v3.insert_sec_facts(self.cursor, company_id=COMPANY_ID, cik=CIK, facts=[instant],
                                     observed_at=OBSERVED, run_id=run)
        self.assertEqual(
            evidence_v3.insert_sec_facts(self.cursor, company_id=COMPANY_ID, cik=CIK, facts=[instant],
                                         observed_at=OBSERVED, run_id=run), (0, 1))

    def test_evidence_tables_are_append_only(self):
        run = self.run_id()
        evidence_v3.insert_sec_facts(self.cursor, company_id=COMPANY_ID, cik=CIK, facts=[fact()],
                                     observed_at=OBSERVED, run_id=run)
        for table, column in (("sec_companyfacts_raw", "value"), ("ingestion_runs", "item_count")):
            with self.subTest(table=table):
                self.expect_database_error(
                    f"UPDATE public.{table} SET {column} = {column} WHERE created_at >= now() - interval '1 minute';")
                self.expect_database_error(
                    f"DELETE FROM public.{table} WHERE created_at >= now() - interval '1 minute';")

    def test_failed_run_requires_symbolic_error_code(self):
        self.run_id("FAILED", "SEC_HTTP_ERROR")
        self.expect_database_error(
            "INSERT INTO public.ingestion_runs (company_id, provider, operation, contract_version, started_at,"
            " completed_at, status, error_code) VALUES (%s, 'SEC', 'x', 'x', now(), now(), 'FAILED', NULL);",
            (COMPANY_ID,))

    def test_origin_is_constrained(self):
        run = self.run_id()
        self.expect_database_error(
            "INSERT INTO public.sec_companyfacts_raw (company_id, cik, taxonomy, tag, unit, period_end, value,"
            " accession, form, filed_date, catalog_version, observed_at, run_id, origin)"
            " VALUES (%s, %s, 'us-gaap', 'X', 'USD', '2000-01-01', 1, 'TEST-X', '10-Q', '2000-02-01', 'x', now(), %s,"
            " 'somewhere.else');",
            (COMPANY_ID, CIK, run))

    def test_filing_metadata_change_conflicts(self):
        run = self.run_id()
        filing = FilingRecord("TEST-0000000002", "10-Q", date(2000, 5, 1),
                              datetime(2000, 5, 1, 21, tzinfo=UTC), date(2000, 3, 31))
        self.assertEqual(evidence_v3.insert_filings(self.cursor, company_id=COMPANY_ID, cik=CIK, filings=[filing],
                                                    observed_at=OBSERVED, run_id=run), (1, 0))
        changed = FilingRecord("TEST-0000000002", "10-Q/A", date(2000, 5, 1),
                               datetime(2000, 5, 1, 21, tzinfo=UTC), date(2000, 3, 31))
        with self.assertRaises(evidence_v3.EvidenceConflict):
            evidence_v3.insert_filings(self.cursor, company_id=COMPANY_ID, cik=CIK, filings=[changed],
                                       observed_at=OBSERVED, run_id=run)

    def test_registrant_link_is_deduplicated_and_status_checked(self):
        run = self.run_id()
        link = RegistrantLink(CIK, "0000000001", "8-K12B", "TEST-LINK", date(2000, 6, 1), "LINKED")
        self.assertTrue(evidence_v3.insert_registrant_link(self.cursor, company_id=COMPANY_ID, link=link,
                                                           evidence={}, observed_at=OBSERVED, run_id=run))
        self.assertFalse(evidence_v3.insert_registrant_link(self.cursor, company_id=COMPANY_ID, link=link,
                                                            evidence={}, observed_at=OBSERVED, run_id=run))
        self.expect_database_error(
            "INSERT INTO public.registrant_links (company_id, successor_cik, predecessor_cik, succession_form,"
            " succession_accession, succession_date, status, observed_at, run_id)"
            " VALUES (%s, %s, NULL, '8-K12B', 'TEST-BAD', '2000-06-01', 'LINKED', now(), %s);",
            (COMPANY_ID, CIK, run))

    def test_point_in_time_reads(self):
        run = self.run_id()
        evidence_v3.insert_sec_facts(self.cursor, company_id=COMPANY_ID, cik=CIK, facts=[fact()],
                                     observed_at=OBSERVED, run_id=run)
        self.cursor.execute(evidence_v3.LOAD_SEC_FACTS_SQL, (COMPANY_ID, OBSERVED - timedelta(seconds=1)))
        self.assertFalse(any(row[7] == "TEST-0000000001" for row in self.cursor.fetchall()))
        self.cursor.execute(evidence_v3.LOAD_SEC_FACTS_SQL, (COMPANY_ID, OBSERVED))
        rows = [row for row in self.cursor.fetchall() if row[7] == "TEST-0000000001"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][14], "sec.company_facts")

    def price_bar(self, close="10.5", *, day=date(2000, 12, 29), observed_at=OBSERVED):
        return PriceBar(bar_date=day, open=Decimal("10"), high=Decimal("11"), low=Decimal("9.5"),
                        close=Decimal(close), adj_close=Decimal("10.4"), volume=1000, currency="USD",
                        observed_at=observed_at)

    def test_price_bars_store_only_changes_and_read_point_in_time(self):
        run = self.run_id()
        insert = lambda bars: evidence_v3.insert_price_bars(
            self.cursor, company_id=COMPANY_ID, ticker="AAPL", bars=bars,
            exchange_timezone="America/New_York", run_id=run)
        self.assertEqual(insert([self.price_bar()]), (1, 0))
        later = OBSERVED + timedelta(days=1)
        self.assertEqual(insert([self.price_bar(observed_at=later)]), (0, 1))
        self.assertEqual(insert([self.price_bar("10.6", observed_at=later)]), (1, 0))
        loaded = [bar for bar in self._load_bars(OBSERVED) if bar.bar_date == date(2000, 12, 29)]
        self.assertEqual([bar.close for bar in loaded], [Decimal("10.5")])
        loaded = [bar for bar in self._load_bars(later) if bar.bar_date == date(2000, 12, 29)]
        self.assertEqual([bar.close for bar in loaded], [Decimal("10.5"), Decimal("10.6")])

    def _load_bars(self, as_of):
        self.cursor.execute(evidence_v3.LOAD_PRICE_BARS_SQL, (COMPANY_ID, as_of, as_of.date()))
        return [PriceBar(bar_date=row[1], open=row[2], high=row[3], low=row[4], close=evidence_v3._plain_decimal(row[5]),
                         adj_close=row[6], volume=int(row[7]), currency=row[8], observed_at=row[9], id=row[0])
                for row in self.cursor.fetchall()]

    def test_price_bar_checks(self):
        run = self.run_id()
        base = ("INSERT INTO public.yahoo_price_bars_raw (company_id, ticker, bar_date, open, high, low, close,"
                " adj_close, volume, currency, exchange_timezone, observed_at, run_id)"
                " VALUES (%s, 'AAPL', %s, %s, %s, %s, %s, 1, 1, 'USD', 'America/New_York', %s, %s);")
        # close above high
        self.expect_database_error(base, (COMPANY_ID, date(2000, 12, 28), 10, 11, 9, 12, OBSERVED, run))
        # a bar dated after its observation day in exchange time
        self.expect_database_error(base, (COMPANY_ID, date(2001, 1, 2), 10, 11, 9, 10, OBSERVED, run))
        evidence_v3.insert_price_bars(self.cursor, company_id=COMPANY_ID, ticker="AAPL", bars=[self.price_bar()],
                                      exchange_timezone="America/New_York", run_id=run)
        self.expect_database_error(
            "UPDATE public.yahoo_price_bars_raw SET close = close WHERE created_at >= now() - interval '1 minute';")

    def test_filing_items_are_stored_once_and_need_their_filing(self):
        run = self.run_id()
        filing = FilingRecord("TEST-0000000003", "8-K", date(2000, 6, 1),
                              datetime(2000, 6, 1, 21, tzinfo=UTC), None, ("5.02", "9.01"))
        evidence_v3.insert_filings(self.cursor, company_id=COMPANY_ID, cik=CIK, filings=[filing],
                                   observed_at=OBSERVED, run_id=run)
        insert = lambda filings: evidence_v3.insert_filing_items(
            self.cursor, company_id=COMPANY_ID, filings=filings, observed_at=OBSERVED, run_id=run)
        self.assertEqual(insert([filing]), 2)
        self.assertEqual(insert([filing]), 0)
        orphan = FilingRecord("TEST-0000000004", "8-K", date(2000, 6, 1), None, None, ("5.02",))
        import psycopg

        with self.assertRaises(psycopg.Error):
            with self.connection.transaction():
                insert([orphan])
        self.cursor.execute(evidence_v3.LOAD_CATALYST_FILINGS_SQL,
                            {"company_id": COMPANY_ID, "as_of": OBSERVED, "since": date(2000, 1, 1)})
        rows = [row for row in self.cursor.fetchall() if row[1] == "TEST-0000000003"]
        self.assertEqual([row[5] for row in rows], ["5.02", "9.01"])


if __name__ == "__main__":
    unittest.main()
