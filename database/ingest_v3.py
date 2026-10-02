"""Repeatable, ticker-agnostic evidence ingestion for fundamentals v3.

Usage::

    python -m database.ingest_v3 AAPL MSFT NVDA:0001045810

A ticker uses the CIK stored in ``companies``; ``TICKER:CIK`` supplies it for a
new company. Either way the CIK is verified against SEC submissions.

Each provider operation is one immutable ingestion run committed together with
the evidence it returned. A failed operation is recorded as FAILED and the
other operations continue; downstream C reports the missing evidence instead
of guessing.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from database import evidence_v3, providers_v3
from database.providers_v3 import ProviderError
from database.sec_facts import CATALOG_VERSION, parse_companyfacts

SEC_FACTS_CONTRACT = f"sec-companyfacts-raw-v1/{CATALOG_VERSION}"
SEC_FILINGS_CONTRACT = "sec-submissions-v1"
YAHOO_CONTRACT = "yahoo-quarterly-raw-v1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _connect(connection_factory: Callable | None):
    if connection_factory is not None:
        return connection_factory()
    from database.db import get_connection

    return get_connection()


def ensure_company(identity: providers_v3.CompanyIdentity, *, connection_factory=None) -> int:
    """Return the company id, creating the row from SEC identity if needed."""

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id, cik FROM public.companies WHERE ticker = %s;", (identity.ticker,))
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise RuntimeError(f"ticker {identity.ticker} is not unique in companies")
        if rows:
            company_id, cik = rows[0]
            if cik is None:
                cursor.execute("UPDATE public.companies SET cik = %s, updated_at = NOW() WHERE id = %s;",
                               (identity.cik, company_id))
            elif cik != identity.cik:
                raise RuntimeError(f"{identity.ticker}: stored CIK {cik} differs from SEC {identity.cik}")
            return company_id
        cursor.execute(
            """INSERT INTO public.companies (ticker, company_name, cik, exchange, country, currency)
               VALUES (%s, %s, %s, %s, 'US', 'USD') RETURNING id;""",
            (identity.ticker, identity.name, identity.cik, identity.exchange or "UNKNOWN"),
        )
        return cursor.fetchone()[0]


def _run_operation(company_id, provider, operation, contract, fetch, persist, *, clock, connection_factory):
    """Fetch, then commit the run record and its evidence in one transaction."""

    started = clock()
    try:
        payload = fetch()
    except ProviderError as error:
        completed = clock()
        with _connect(connection_factory) as connection, connection.cursor() as cursor:
            evidence_v3.record_run(
                cursor, company_id=company_id, provider=provider, operation=operation,
                contract_version=contract, started_at=started, completed_at=completed,
                status="FAILED", error_code=error.code,
            )
        return {"operation": operation, "status": "FAILED", "error_code": error.code}
    completed = clock()
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        summary = persist(cursor, payload, started, completed)
    return {"operation": operation, "status": "SUCCESS", **summary}


def ingest_company(
    ticker: str,
    *,
    cik: str | None = None,
    clock: Callable[[], datetime] = _utcnow,
    connection_factory: Callable | None = None,
    sec_getter: Callable | None = None,
    yahoo_fetcher: Callable | None = None,
    stock_factory: Callable | None = None,
    split_acquirer: Callable | None = None,
) -> dict:
    ticker = ticker.strip().upper()
    if cik is None:
        stored = evidence_v3.load_company(ticker, connection_factory=connection_factory)
        cik = stored[1] if stored else None
    identity = providers_v3.resolve_company(ticker, known_cik=cik, getter=sec_getter)
    company_id = ensure_company(identity, connection_factory=connection_factory)
    steps = []

    def persist_facts(cursor, payload, started, completed):
        facts = parse_companyfacts(payload)
        run_id = evidence_v3.record_run(
            cursor, company_id=company_id, provider="SEC", operation="sec.company_facts",
            contract_version=SEC_FACTS_CONTRACT, started_at=started, completed_at=completed,
            status="SUCCESS", error_code=None, item_count=len(facts),
        )
        inserted, existing = evidence_v3.insert_sec_facts(
            cursor, company_id=company_id, cik=identity.cik, facts=facts, observed_at=completed, run_id=run_id,
        )
        return {"items": len(facts), "inserted": inserted, "existing": existing}

    steps.append(_run_operation(
        company_id, "SEC", "sec.company_facts", SEC_FACTS_CONTRACT,
        lambda: providers_v3.fetch_companyfacts(identity.cik, getter=sec_getter), persist_facts,
        clock=clock, connection_factory=connection_factory,
    ))

    def persist_filings(cursor, filings, started, completed):
        run_id = evidence_v3.record_run(
            cursor, company_id=company_id, provider="SEC", operation="sec.submissions",
            contract_version=SEC_FILINGS_CONTRACT, started_at=started, completed_at=completed,
            status="SUCCESS", error_code=None, item_count=len(filings),
        )
        inserted, existing = evidence_v3.insert_filings(
            cursor, company_id=company_id, cik=identity.cik, filings=filings, observed_at=completed, run_id=run_id,
        )
        return {"items": len(filings), "inserted": inserted, "existing": existing}

    steps.append(_run_operation(
        company_id, "SEC", "sec.submissions", SEC_FILINGS_CONTRACT,
        lambda: providers_v3.fetch_filings(identity.cik, getter=sec_getter), persist_filings,
        clock=clock, connection_factory=connection_factory,
    ))

    steps.append(_ingest_yahoo_timeseries(company_id, ticker, clock, connection_factory, yahoo_fetcher))
    steps.append(_ingest_yfinance_income(company_id, ticker, clock, connection_factory, stock_factory))

    acquirer = split_acquirer
    if acquirer is None:
        from database.corporate_action_acquisition import acquire_and_record_stock_splits as acquirer
    split_result = acquirer(company_id, ticker, clock())
    steps.append({
        "operation": "yfinance.splits",
        "status": split_result["acquisition_status"],
        "events": len(split_result["events"]),
        "outcome": split_result["repository_outcome"],
    })
    return {"ticker": ticker, "company_id": company_id, "cik": identity.cik, "steps": steps}


def _persist_yahoo_facts(company_id, operation, facts, cursor, started, completed):
    from database.fundamentals import _insert_raw_with_cursor

    run_id = evidence_v3.record_run(
        cursor, company_id=company_id, provider="YAHOO_FINANCE", operation=operation,
        contract_version=YAHOO_CONTRACT, started_at=started, completed_at=completed,
        status="SUCCESS", error_code=None, item_count=len(facts),
    )
    raw_ids = [_insert_raw_with_cursor(cursor, {**fact, "fetched_at": completed}) for fact in facts]
    evidence_v3.insert_run_items(cursor, run_id, raw_ids)
    return {"items": len(facts), "raw_rows": len(set(raw_ids))}


def _ingest_yahoo_timeseries(company_id, ticker, clock, connection_factory, fetcher):
    from database.yahoo_import import TIMESERIES_VARIANT, YAHOO_TYPES, _raw_fact

    def fetch():
        return providers_v3.fetch_yahoo_timeseries(ticker, fetcher=fetcher)

    def persist(cursor, series_by_type, started, completed):
        facts = []
        for series_type, (metric, unit) in YAHOO_TYPES.items():
            for item_date, value in series_by_type[series_type].dropna().sort_index().items():
                period_end = item_date.date()
                facts.append(_raw_fact(
                    company_id, TIMESERIES_VARIANT, f"{series_type}:{period_end.isoformat()}",
                    metric, period_end, value, unit, completed, series_type,
                ))
        return _persist_yahoo_facts(company_id, TIMESERIES_VARIANT, facts, cursor, started, completed)

    return _run_operation(company_id, "YAHOO_FINANCE", TIMESERIES_VARIANT, YAHOO_CONTRACT, fetch, persist,
                          clock=clock, connection_factory=connection_factory)


def _ingest_yfinance_income(company_id, ticker, clock, connection_factory, stock_factory):
    from database.yahoo_import import METRIC_UNITS, YFINANCE_ALIASES, YFINANCE_VARIANT, _extract_named_row, _raw_fact

    def fetch():
        return providers_v3.fetch_yfinance_income(ticker, stock_factory=stock_factory)

    def persist(cursor, frame, started, completed):
        facts = []
        for metric, aliases in YFINANCE_ALIASES.items():
            name, series = _extract_named_row(frame, aliases)
            if series is None:
                continue
            for item_date, value in series.dropna().sort_index().items():
                period_end = item_date.date()
                facts.append(_raw_fact(
                    company_id, YFINANCE_VARIANT, f"{name}:{period_end.isoformat()}",
                    metric, period_end, value, METRIC_UNITS[metric], completed, name,
                ))
        return _persist_yahoo_facts(company_id, YFINANCE_VARIANT, facts, cursor, started, completed)

    return _run_operation(company_id, "YAHOO_FINANCE", YFINANCE_VARIANT, YAHOO_CONTRACT, fetch, persist,
                          clock=clock, connection_factory=connection_factory)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest v3 evidence for one or more tickers")
    parser.add_argument("tickers", nargs="+", help="TICKER or TICKER:CIK")
    args = parser.parse_args(argv)
    results = []
    for item in args.tickers:
        ticker, _, cik = item.partition(":")
        results.append(ingest_company(ticker, cik=cik or None))
    print(json.dumps(results, indent=2, default=str))
    return results


if __name__ == "__main__":
    main()
