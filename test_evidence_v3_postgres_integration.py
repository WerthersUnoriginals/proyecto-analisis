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


if __name__ == "__main__":
    unittest.main()
