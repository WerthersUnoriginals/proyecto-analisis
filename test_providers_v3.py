"""Offline tests for v3 provider parsing and ingestion orchestration."""

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from database import ingest_v3
from database.providers_v3 import (
    ProviderError,
    fetch_companyfacts,
    fetch_filings,
    fetch_yahoo_timeseries,
    fetch_yfinance_income,
    parse_company_tickers,
    parse_submission_arrays,
)

UTC = timezone.utc
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TICKERS = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [[1045810, "NVIDIA CORP", "NVDA", "Nasdaq"], [320193, "Apple Inc.", "AAPL", "Nasdaq"]],
}


def arrays(*rows):
    keys = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form")
    return {key: [row[index] for row in rows] for index, key in enumerate(keys)}


class SecParsingTests(unittest.TestCase):
    def test_company_identity_is_padded_and_unique(self):
        identity = parse_company_tickers(TICKERS, "nvda")
        self.assertEqual((identity.cik, identity.name, identity.exchange), ("0001045810", "NVIDIA CORP", "NASDAQ"))
        with self.assertRaises(ProviderError) as missing:
            parse_company_tickers(TICKERS, "ZZZZ")
        self.assertEqual(missing.exception.code, "CIK_NOT_FOUND")

    def test_acceptance_is_utc(self):
        records = parse_submission_arrays(arrays(
            ("0001045810-26-000020", "2026-08-26", "2026-07-26", "2026-08-26T20:36:00.000Z", "10-Q"),
        ))
        self.assertEqual(records[0].acceptance_at, datetime(2026, 8, 26, 20, 36, tzinfo=UTC))
        self.assertEqual(records[0].report_date, date(2026, 7, 26))

    def test_shape_mismatch_fails(self):
        broken = arrays(("a", "2026-01-01", "", None, "10-Q"))
        broken["form"] = []
        with self.assertRaises(ProviderError):
            parse_submission_arrays(broken)

    def test_filings_include_paged_files(self):
        pages = {
            "https://data.sec.gov/submissions/CIK0000000001.json": {"filings": {
                "recent": arrays(("b", "2026-01-02", "", "2026-01-02T21:00:00.000Z", "10-Q")),
                "files": [{"name": "CIK0000000001-submissions-001.json"}],
            }},
            "https://data.sec.gov/submissions/CIK0000000001-submissions-001.json":
                arrays(("a", "2020-01-02", "", "2020-01-02T21:00:00.000Z", "10-K")),
        }
        records = fetch_filings("0000000001", getter=pages.__getitem__)
        self.assertEqual([record.accession for record in records], ["a", "b"])

    def test_known_cik_is_verified_against_submissions(self):
        from database.providers_v3 import identity_from_submissions

        payload = {"cik": "0001045810", "name": "NVIDIA CORP", "tickers": ["NVDA"], "exchanges": ["Nasdaq"]}
        identity = identity_from_submissions(payload, "nvda", "0001045810")
        self.assertEqual((identity.name, identity.exchange), ("NVIDIA CORP", "NASDAQ"))
        with self.assertRaises(ProviderError) as error:
            identity_from_submissions(payload, "AAPL", "0001045810")
        self.assertEqual(error.exception.code, "TICKER_NOT_LISTED_FOR_CIK")

    def test_companyfacts_cik_must_match(self):
        with self.assertRaises(ProviderError) as error:
            fetch_companyfacts("0000000001", getter=lambda url: {"cik": 2, "facts": {}})
        self.assertEqual(error.exception.code, "SEC_COMPANYFACTS_CIK_MISMATCH")


class YahooTests(unittest.TestCase):
    def test_partial_timeseries_is_a_failure(self):
        fetcher = lambda ticker, types, years: ({"quarterlyDilutedEPS": pd.Series(dtype=float)}, "revenue failed")
        with self.assertRaises(ProviderError) as error:
            fetch_yahoo_timeseries("AAPL", fetcher=fetcher)
        self.assertEqual(error.exception.code, "YAHOO_TIMESERIES_INCOMPLETE")

    def test_empty_income_statement_is_a_failure(self):
        class Stock:
            quarterly_income_stmt = pd.DataFrame()

        with self.assertRaises(ProviderError) as error:
            fetch_yfinance_income("AAPL", stock_factory=lambda ticker: Stock())
        self.assertEqual(error.exception.code, "YFINANCE_EMPTY_RESPONSE")


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.db.statements.append((text, params))
        if text.startswith("SELECT id, cik FROM public.companies"):
            self.result = [(7, "0001045810")]
        elif "RETURNING id" in text:
            self.db.next_id += 1
            self.result = [(self.db.next_id,)]
        elif "FROM public.sec_filings AS f" in text:
            self.result = list(self.db.pending)
        elif text.startswith("SELECT form, filing_date, accession FROM public.sec_filings"):
            self.result = list(self.db.registrant_filings) if params[1] == "0001045810" else []
        elif text.startswith("SELECT id FROM fundamentals_raw"):
            self.result = [(1,)]
        else:
            self.result = []

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result or [])


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.transactions += 1
        return False

    def cursor(self):
        return FakeCursor(self.db)


class FakeDb:
    def __init__(self):
        self.statements = []
        self.next_id = 100
        self.transactions = 0
        self.pending = []
        self.registrant_filings = []

    def connect(self):
        return FakeConnection(self)

    def runs(self):
        return [params for sql, params in self.statements if sql.startswith("INSERT INTO public.ingestion_runs")]


class IngestOrchestrationTests(unittest.TestCase):
    def sec_getter(self, failing=()):
        payload = json.loads((FIXTURES / "sec_companyfacts_nvda_2026-10-02.json").read_text(encoding="utf-8"))

        def getter(url):
            if url.endswith("-index-headers.html"):
                return (b"FILER:\n COMPANY CONFORMED NAME: OLD CO\n CENTRAL INDEX KEY: 0000034088\n"
                        b"FILER:\n COMPANY CONFORMED NAME: NEW CO\n CENTRAL INDEX KEY: 0001045810\n")
            if "CIK0000034088.json" in url and "submissions" in url:
                return {"cik": "0000034088", "name": "OLD CO", "tickers": [], "exchanges": [],
                        "filings": {"recent": arrays(("old-k", "2026-02-18", "", "2026-02-18T21:00:00.000Z", "10-K"))}}
            if "CIK0000034088.json" in url and "companyfacts" in url:
                return {"cik": 34088, "facts": {}}
            if url.endswith("/index.json"):
                return {"directory": {"item": [{"name": "nee-20260630_htm.xml"}, {"name": "x.htm"}]}}
            if url.endswith("_htm.xml"):
                return self.instance
            if "company_tickers" in url:
                return TICKERS
            if "companyfacts" in url:
                if "companyfacts" in failing:
                    raise ProviderError("SEC_HTTP_ERROR")
                return payload
            return {
                "cik": "0001045810", "name": "NVIDIA CORP", "tickers": ["NVDA"], "exchanges": ["Nasdaq"],
                "filings": {"recent": arrays(("a", "2026-08-26", "", "2026-08-26T20:36:00.000Z", "10-Q"))},
            }
        return getter

    def run_ingest(self, failing=(), pending=(), instance=None, registrant_filings=()):
        db = FakeDb()
        db.pending = list(pending)
        db.registrant_filings = list(registrant_filings)
        clock = iter(datetime(2026, 10, 2, 12, minute, tzinfo=UTC) for minute in range(60))
        index = pd.to_datetime(["2026-06-30"])
        series = {name: pd.Series([1.0], index=index) for name in
                  ("quarterlyDilutedEPS", "quarterlyTotalRevenue", "quarterlyNetIncome")}

        class Stock:
            quarterly_income_stmt = pd.DataFrame()

        self.instance = instance
        result = ingest_v3.ingest_company(
            "NVDA", clock=lambda: next(clock), connection_factory=db.connect,
            sec_getter=self.sec_getter(failing),
            yahoo_fetcher=lambda ticker, types, years: (series, None),
            stock_factory=lambda ticker: Stock(),
            split_acquirer=lambda company_id, ticker, as_of: {
                "acquisition_status": "SUCCESS", "events": (), "repository_outcome": "PERSISTED",
            },
        )
        return db, result

    def test_every_operation_is_recorded_including_failures(self):
        db, result = self.run_ingest(failing=("companyfacts",))
        statuses = {step["operation"]: step["status"] for step in result["steps"]}
        self.assertEqual(statuses["sec.company_facts"], "FAILED")
        self.assertEqual(statuses["sec.submissions"], "SUCCESS")
        self.assertEqual(statuses["yahoo.fundamentals_timeseries"], "SUCCESS")
        self.assertEqual(statuses["yfinance.quarterly_income_stmt"], "FAILED")
        recorded = [(params[2], params[6], params[7]) for params in db.runs()]
        self.assertIn(("sec.company_facts", "FAILED", "SEC_HTTP_ERROR"), recorded)
        self.assertIn(("yfinance.quarterly_income_stmt", "FAILED", "YFINANCE_EMPTY_RESPONSE"), recorded)

    def test_successful_facts_run_writes_every_catalog_fact(self):
        db, result = self.run_ingest()
        facts_step = next(step for step in result["steps"] if step["operation"] == "sec.company_facts")
        inserts = [sql for sql, _ in db.statements if sql.startswith("INSERT INTO public.sec_companyfacts_raw")]
        self.assertEqual(len(inserts), facts_step["items"])
        self.assertGreater(facts_step["items"], 1000)

    def test_lagging_filing_is_read_from_its_instance(self):
        from test_sec_xbrl_instance import INSTANCE

        instance = INSTANCE.replace(b"0000753308", b"0001045810")
        db, result = self.run_ingest(pending=[("0001045810-26-000099", "10-Q", date(2026, 8, 26))],
                                     instance=instance)
        step = next(step for step in result["steps"] if step["operation"] == "sec.xbrl_instance")
        self.assertEqual((step["status"], step["items"]), ("SUCCESS", 4))
        inserts = [params for sql, params in db.statements if sql.startswith("INSERT INTO public.sec_companyfacts_raw")]
        self.assertTrue(all(params[-1] == "sec.xbrl_instance" for params in inserts[-4:]))

    def test_corrupt_instance_is_recorded_not_raised(self):
        db, result = self.run_ingest(pending=[("0001045810-26-000099", "10-Q", date(2026, 8, 26))],
                                     instance=b"<not xml")
        step = next(step for step in result["steps"] if step["operation"] == "sec.xbrl_instance")
        self.assertEqual((step["status"], step["error_code"]), ("FAILED", "EVIDENCE_INVALID"))

    def test_succession_links_predecessor_and_ingests_its_history(self):
        filings = [("8-K12B", date(2026, 7, 1), "succ"), ("10-Q", date(2026, 8, 3), "joint-q")]
        db, result = self.run_ingest(registrant_filings=filings)
        succession = next(step for step in result["steps"] if step["operation"] == "sec.succession")
        self.assertEqual((succession["status"], succession["linked"]), ("SUCCESS", ["0000034088"]))
        links = [params for sql, params in db.statements if sql.startswith("INSERT INTO public.registrant_links")]
        self.assertEqual((links[0][2], links[0][6]), ("0000034088", "LINKED"))
        predecessor_steps = [step for step in result["steps"] if step.get("cik") == "0000034088"]
        self.assertEqual({step["operation"] for step in predecessor_steps}, {"sec.company_facts", "sec.submissions"})

    def test_succession_without_joint_predecessor_is_recorded_unresolved(self):
        filings = [("8-K12B", date(2026, 7, 1), "succ")]
        db, result = self.run_ingest(registrant_filings=filings)
        links = [params for sql, params in db.statements if sql.startswith("INSERT INTO public.registrant_links")]
        self.assertEqual((links[0][2], links[0][6]), (None, "PREDECESSOR_NOT_FOUND"))

    def test_yahoo_run_records_its_items(self):
        db, _ = self.run_ingest()
        items = [params for sql, params in db.statements if sql.startswith("INSERT INTO public.ingestion_run_items")]
        self.assertEqual(len(items), 1)  # three facts deduplicated by the fake to one raw row


if __name__ == "__main__":
    unittest.main()
