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
from database.sec_xbrl_instance import parse_xbrl_instance

SEC_FACTS_CONTRACT = f"sec-companyfacts-raw-v1/{CATALOG_VERSION}"
SEC_FILINGS_CONTRACT = "sec-submissions-v3"  # 8-K items (v2) and acceptance observations (v3)
SEC_SUCCESSION_CONTRACT = "sec-succession-v1"
SUCCESSION_JOINT_WINDOW_DAYS = (30, 400)
SUCCESSION_MAX_HEADERS = 4
SUCCESSION_LOOKBACK_DAYS = round(365.25 * 8)
SEC_INSTANCE_CONTRACT = f"sec-xbrl-instance-v1/{CATALOG_VERSION}"
INSTANCE_LOOKBACK_DAYS = 730
YAHOO_CONTRACT = "yahoo-quarterly-raw-v1"
YAHOO_BARS_CONTRACT = "yahoo-daily-bars-raw-v1"
YAHOO_BARS_OPERATION = "yfinance.daily_bars"


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
    try:
        with _connect(connection_factory) as connection, connection.cursor() as cursor:
            summary = persist(cursor, payload, started, completed)
    except (evidence_v3.EvidenceConflict, ValueError) as error:
        # Contradictory or unreadable evidence: keep stored evidence untouched
        # and record the attempt for audit.
        code = "EVIDENCE_CONFLICT" if isinstance(error, evidence_v3.EvidenceConflict) else "EVIDENCE_INVALID"
        with _connect(connection_factory) as connection, connection.cursor() as cursor:
            evidence_v3.record_run(
                cursor, company_id=company_id, provider=provider, operation=operation,
                contract_version=contract, started_at=started, completed_at=clock(),
                status="FAILED", error_code=code,
            )
        return {"operation": operation, "status": "FAILED", "error_code": code}
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
    steps = [
        _ingest_companyfacts(company_id, identity.cik, clock, connection_factory, sec_getter),
        _ingest_submissions(company_id, identity.cik, clock, connection_factory, sec_getter),
    ]
    succession_steps, predecessors = _ingest_successions(
        company_id, identity.cik, clock, connection_factory, sec_getter,
    )
    steps.extend(succession_steps)
    for predecessor in predecessors:
        steps.append({**_ingest_companyfacts(company_id, predecessor, clock, connection_factory, sec_getter),
                      "cik": predecessor})
        steps.append({**_ingest_submissions(company_id, predecessor, clock, connection_factory, sec_getter),
                      "cik": predecessor})
    steps.extend(_ingest_lagging_instances(
        company_id, identity.cik, clock, connection_factory, sec_getter, predecessors=predecessors,
    ))
    steps.append(_ingest_yahoo_timeseries(company_id, ticker, clock, connection_factory, yahoo_fetcher))
    steps.append(_ingest_yfinance_income(company_id, ticker, clock, connection_factory, stock_factory))
    steps.append(_ingest_daily_bars(company_id, ticker, clock, connection_factory, stock_factory))

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


def _ingest_companyfacts(company_id, cik, clock, connection_factory, sec_getter):
    def persist(cursor, payload, started, completed):
        facts = parse_companyfacts(payload)
        run_id = evidence_v3.record_run(
            cursor, company_id=company_id, provider="SEC", operation="sec.company_facts",
            contract_version=SEC_FACTS_CONTRACT, started_at=started, completed_at=completed,
            status="SUCCESS", error_code=None, item_count=len(facts), metadata={"cik": cik},
        )
        inserted, existing = evidence_v3.insert_sec_facts(
            cursor, company_id=company_id, cik=cik, facts=facts, observed_at=completed, run_id=run_id,
        )
        return {"items": len(facts), "inserted": inserted, "existing": existing}

    return _run_operation(
        company_id, "SEC", "sec.company_facts", SEC_FACTS_CONTRACT,
        lambda: providers_v3.fetch_companyfacts(cik, getter=sec_getter), persist,
        clock=clock, connection_factory=connection_factory,
    )


def _ingest_submissions(company_id, cik, clock, connection_factory, sec_getter):
    def persist(cursor, filings, started, completed):
        run_id = evidence_v3.record_run(
            cursor, company_id=company_id, provider="SEC", operation="sec.submissions",
            contract_version=SEC_FILINGS_CONTRACT, started_at=started, completed_at=completed,
            status="SUCCESS", error_code=None, item_count=len(filings), metadata={"cik": cik},
        )
        inserted, existing = evidence_v3.insert_filings(
            cursor, company_id=company_id, cik=cik, filings=filings, observed_at=completed, run_id=run_id,
        )
        new_acceptances = evidence_v3.insert_acceptance_observations(
            cursor, company_id=company_id, filings=filings, observed_at=completed, run_id=run_id,
        )
        new_items = evidence_v3.insert_filing_items(
            cursor, company_id=company_id, filings=filings, observed_at=completed, run_id=run_id,
        )
        return {"items": len(filings), "inserted": inserted, "existing": existing,
                "new_acceptance_values": new_acceptances, "new_8k_items": new_items}

    return _run_operation(
        company_id, "SEC", "sec.submissions", SEC_FILINGS_CONTRACT,
        lambda: providers_v3.fetch_filings(cik, getter=sec_getter), persist,
        clock=clock, connection_factory=connection_factory,
    )


def _ingest_successions(company_id, successor_cik, clock, connection_factory, sec_getter):
    """Detect successor registrants and link verified predecessors (sec-succession-v1)."""

    from datetime import timedelta

    from database.registrant_v3 import (
        PERIODIC_FORMS, RegistrantLink, choose_predecessors, succession_filings,
    )

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        filings = evidence_v3.load_registrant_filings(cursor, company_id, successor_cik)
    steps, predecessors = [], []
    horizon = clock().date() - timedelta(days=SUCCESSION_LOOKBACK_DAYS)
    for form, filed, accession in succession_filings(filings):
        if filed < horizon:
            continue
        low = filed - timedelta(days=SUCCESSION_JOINT_WINDOW_DAYS[0])
        high = filed + timedelta(days=SUCCESSION_JOINT_WINDOW_DAYS[1])
        periodic = sorted(
            (item for item in filings if item[0] in PERIODIC_FORMS and low <= item[1] <= high),
            key=lambda item: item[1],
        )[:SUCCESSION_MAX_HEADERS]

        def fetch(periodic=periodic):
            joint = {
                item[2]: [cik for cik, _ in providers_v3.fetch_filing_filers(successor_cik, item[2], bytes_getter=sec_getter)]
                for item in periodic
            }
            candidates = sorted({cik for ciks in joint.values() for cik in ciks if cik != successor_cik})
            histories = {
                cik: [(record.form, record.filing_date) for record in providers_v3.fetch_filings(cik, getter=sec_getter)]
                for cik in candidates
            }
            return joint, histories

        def persist(cursor, payload, started, completed, form=form, filed=filed, accession=accession):
            joint, histories = payload
            chosen = choose_predecessors(successor_cik, filed, joint, histories)
            links = [
                RegistrantLink(successor_cik, cik, form, accession, filed, "LINKED") for cik in chosen
            ] or [RegistrantLink(successor_cik, None, form, accession, filed, "PREDECESSOR_NOT_FOUND")]
            run_id = evidence_v3.record_run(
                cursor, company_id=company_id, provider="SEC", operation="sec.succession",
                contract_version=SEC_SUCCESSION_CONTRACT, started_at=started, completed_at=completed,
                status="SUCCESS", error_code=None, item_count=len(links),
                metadata={"succession_accession": accession},
            )
            for link in links:
                evidence_v3.insert_registrant_link(
                    cursor, company_id=company_id, link=link,
                    evidence={"joint_filers": joint, "candidates": sorted(histories)},
                    observed_at=completed, run_id=run_id,
                )
            predecessors.extend(chosen)
            return {"succession_accession": accession, "linked": chosen}

        steps.append(_run_operation(
            company_id, "SEC", "sec.succession", SEC_SUCCESSION_CONTRACT, fetch, persist,
            clock=clock, connection_factory=connection_factory,
        ))
    return steps, sorted(set(predecessors))


def _ingest_lagging_instances(company_id, cik, clock, connection_factory, sec_getter, predecessors=()):
    """Read the XBRL instance of recent periodic filings missing from Company Facts."""

    from datetime import timedelta

    since = clock().date() - timedelta(days=INSTANCE_LOOKBACK_DAYS)
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        pending = evidence_v3.load_periodic_filings_without_facts(cursor, company_id, since)
    steps = []
    for accession, form, filed in pending:
        def fetch(accession=accession):
            return providers_v3.fetch_filing_instance(cik, accession, getter=sec_getter, bytes_getter=sec_getter)

        def persist(cursor, content, started, completed, accession=accession, form=form, filed=filed):
            facts = []
            for registrant in (cik, *predecessors):
                # A joint filing tags its contexts with one of its registrants.
                facts = parse_xbrl_instance(content, cik=registrant, accession=accession, form=form, filed_date=filed)
                if facts:
                    break
            run_id = evidence_v3.record_run(
                cursor, company_id=company_id, provider="SEC", operation="sec.xbrl_instance",
                contract_version=SEC_INSTANCE_CONTRACT, started_at=started, completed_at=completed,
                status="SUCCESS", error_code=None, item_count=len(facts), metadata={"accession": accession},
            )
            inserted, existing = evidence_v3.insert_sec_facts(
                cursor, company_id=company_id, cik=cik, facts=facts, observed_at=completed, run_id=run_id,
            )
            return {"accession": accession, "items": len(facts), "inserted": inserted, "existing": existing}

        steps.append(_run_operation(
            company_id, "SEC", "sec.xbrl_instance", SEC_INSTANCE_CONTRACT, fetch, persist,
            clock=clock, connection_factory=connection_factory,
        ))
    return steps


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


def _ingest_daily_bars(company_id, ticker, clock, connection_factory, stock_factory):
    from database.prices_v3 import bars_from_rows
    from database.quarterly_v3 import six_years_before

    start = six_years_before(clock().date())

    def fetch():
        return providers_v3.fetch_yahoo_daily_bars(ticker, start=start, stock_factory=stock_factory)

    def persist(cursor, payload, started, completed):
        bars, dropped = bars_from_rows(
            payload["rows"], observed_at=completed,
            exchange_timezone=payload["exchange_timezone"], currency=payload["currency"],
        )
        run_id = evidence_v3.record_run(
            cursor, company_id=company_id, provider="YAHOO_FINANCE", operation=YAHOO_BARS_OPERATION,
            contract_version=YAHOO_BARS_CONTRACT, started_at=started, completed_at=completed,
            status="SUCCESS", error_code=None, item_count=len(bars),
            metadata={"start": start.isoformat(), "currency": payload["currency"],
                      "exchange_timezone": payload["exchange_timezone"], **dropped},
        )
        inserted, unchanged = evidence_v3.insert_price_bars(
            cursor, company_id=company_id, ticker=ticker, bars=bars,
            exchange_timezone=payload["exchange_timezone"], run_id=run_id,
        )
        return {"items": len(bars), "inserted": inserted, "unchanged": unchanged,
                "partial_bars": dropped["partial_bars"], "rejected_bars": len(dropped["rejected_bars"])}

    return _run_operation(company_id, "YAHOO_FINANCE", YAHOO_BARS_OPERATION, YAHOO_BARS_CONTRACT, fetch, persist,
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
