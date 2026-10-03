"""Append-only persistence and point-in-time loading of v3 evidence.

Writers take an open cursor so that one provider attempt (its run record and
all the evidence it returned) commits atomically. Loaders return only
evidence observed at or before ``as_of``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Sequence

from database.providers_v3 import FilingRecord
from database.sec_facts import CATALOG_VERSION, SecFact


class EvidenceConflict(RuntimeError):
    """The same evidence identity was published with a different value."""


def _connect(connection_factory: Callable | None):
    if connection_factory is not None:
        return connection_factory()
    from database.db import get_connection

    return get_connection()


def _jsonb(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


INSERT_RUN_SQL = """
    INSERT INTO public.ingestion_runs (
        company_id, provider, operation, contract_version, started_at,
        completed_at, status, error_code, item_count, metadata
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING id;
"""


def record_run(
    cursor, *, company_id: int, provider: str, operation: str, contract_version: str,
    started_at: datetime, completed_at: datetime, status: str, error_code: str | None,
    item_count: int = 0, metadata: Mapping | None = None,
) -> int:
    cursor.execute(INSERT_RUN_SQL, (
        company_id, provider, operation, contract_version, started_at, completed_at,
        status, error_code, item_count, _jsonb(dict(metadata or {})),
    ))
    return cursor.fetchone()[0]


INSERT_SEC_FACT_SQL = """
    INSERT INTO public.sec_companyfacts_raw (
        company_id, cik, taxonomy, tag, unit, period_start, period_end, value,
        accession, fiscal_year, fiscal_period, form, filed_date, frame,
        catalog_version, observed_at, run_id, origin
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
    RETURNING id;
"""

FIND_SEC_FACT_SQL = """
    SELECT id, value FROM public.sec_companyfacts_raw
    WHERE company_id = %s AND taxonomy = %s AND tag = %s AND unit = %s
      AND period_start IS NOT DISTINCT FROM %s AND period_end = %s AND accession = %s;
"""


def insert_sec_facts(
    cursor, *, company_id: int, cik: str, facts: Iterable[SecFact], observed_at: datetime, run_id: int,
) -> tuple[int, int]:
    inserted = existing = 0
    for fact in facts:
        cursor.execute(INSERT_SEC_FACT_SQL, (
            company_id, cik, fact.taxonomy, fact.tag, fact.unit, fact.period_start, fact.period_end,
            fact.value, fact.accession, fact.fiscal_year, fact.fiscal_period, fact.form,
            fact.filed_date, fact.frame, CATALOG_VERSION, observed_at, run_id, fact.origin,
        ))
        if cursor.fetchone() is not None:
            inserted += 1
            continue
        cursor.execute(FIND_SEC_FACT_SQL, (
            company_id, fact.taxonomy, fact.tag, fact.unit, fact.period_start, fact.period_end, fact.accession,
        ))
        row = cursor.fetchone()
        if row is None or Decimal(row[1]) != fact.value:
            raise EvidenceConflict(f"SEC fact changed after publication: {fact.identity}")
        existing += 1
    return inserted, existing


INSERT_FILING_SQL = """
    INSERT INTO public.sec_filings (
        company_id, cik, accession, form, filing_date, acceptance_at, report_date, observed_at, run_id
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (company_id, accession) DO NOTHING
    RETURNING id;
"""

FIND_FILING_SQL = """
    SELECT form, filing_date FROM public.sec_filings
    WHERE company_id = %s AND accession = %s;
"""


def insert_filings(
    cursor, *, company_id: int, cik: str, filings: Iterable[FilingRecord], observed_at: datetime, run_id: int,
) -> tuple[int, int]:
    inserted = existing = 0
    for filing in filings:
        cursor.execute(INSERT_FILING_SQL, (
            company_id, cik, filing.accession, filing.form, filing.filing_date,
            filing.acceptance_at, filing.report_date, observed_at, run_id,
        ))
        if cursor.fetchone() is not None:
            inserted += 1
            continue
        cursor.execute(FIND_FILING_SQL, (company_id, filing.accession))
        row = cursor.fetchone()
        # Acceptance times are not compared: SEC serves inconsistent values,
        # kept as observations (insert_acceptance_observations).
        if row is None or (row[0], row[1]) != (filing.form, filing.filing_date):
            raise EvidenceConflict(f"SEC filing metadata changed: {filing.accession}")
        existing += 1
    return inserted, existing


def insert_run_items(cursor, run_id: int, raw_ids: Sequence[int], raw_table: str = "fundamentals_raw") -> None:
    for raw_id in dict.fromkeys(raw_ids):
        cursor.execute(
            "INSERT INTO public.ingestion_run_items (run_id, raw_table, raw_id) VALUES (%s, %s, %s);",
            (run_id, raw_table, raw_id),
        )


def _plain_decimal(value) -> Decimal:
    """Drop NUMERIC padding zeros without switching to exponent notation."""

    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text or "0")


LOAD_SEC_FACTS_SQL = """
    SELECT id, taxonomy, tag, unit, period_start, period_end, value, accession,
           fiscal_year, fiscal_period, form, filed_date, frame, observed_at, origin
    FROM public.sec_companyfacts_raw
    WHERE company_id = %s AND observed_at <= %s
    ORDER BY id;
"""


def load_sec_facts(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None) -> list[SecFact]:
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_SEC_FACTS_SQL, (company_id, as_of))
        return [
            SecFact(
                taxonomy=row[1], tag=row[2], unit=row[3], period_start=row[4], period_end=row[5],
                value=_plain_decimal(row[6]),
                accession=row[7], fiscal_year=row[8], fiscal_period=row[9], form=row[10],
                filed_date=row[11], frame=row[12], observed_at=row[13], id=row[0], origin=row[14],
            )
            for row in cursor.fetchall()
        ]


# Rows returned by v3 runs, plus pre-v3 Yahoo rows treated as a snapshot of
# their own fetch (they were written by one-shot imports with no run record).
LOAD_YAHOO_ROWS_SQL = """
    SELECT r.id, r.metric, r.period_end, r.value,
           r.source_payload ->> 'source_variant' AS source_variant,
           r.source_payload ->> 'source_metric_name' AS source_metric_name,
           run.completed_at AS run_observed_at
    FROM public.ingestion_runs AS run
    JOIN public.ingestion_run_items AS item ON item.run_id = run.id
    JOIN public.fundamentals_raw AS r ON r.id = item.raw_id
    WHERE run.company_id = %(company_id)s AND run.provider = 'YAHOO_FINANCE'
      AND run.status = 'SUCCESS' AND run.completed_at <= %(as_of)s
    UNION ALL
    SELECT r.id, r.metric, r.period_end, r.value,
           r.source_payload ->> 'source_variant',
           r.source_payload ->> 'source_metric_name',
           r.fetched_at
    FROM public.fundamentals_raw AS r
    WHERE r.company_id = %(company_id)s AND r.source = 'YAHOO' AND r.fetched_at <= %(as_of)s
      AND r.created_at < (SELECT COALESCE(MIN(created_at), 'infinity') FROM public.ingestion_runs)
"""


def load_yahoo_rows(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None) -> list[dict]:
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_YAHOO_ROWS_SQL, {"company_id": company_id, "as_of": as_of})
        columns = [description.name for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


LOAD_PERIODIC_WITHOUT_FACTS_SQL = """
    SELECT f.accession, f.form, f.filing_date
    FROM public.sec_filings AS f
    WHERE f.company_id = %s
      AND f.form IN ('10-Q', '10-Q/A', '10-K', '10-K/A')
      AND f.filing_date >= %s
      AND NOT EXISTS (
          SELECT 1 FROM public.sec_companyfacts_raw AS r
          WHERE r.company_id = f.company_id AND r.accession = f.accession
      )
    ORDER BY f.filing_date;
"""


def load_periodic_filings_without_facts(cursor, company_id: int, since: date) -> list[tuple[str, str, date]]:
    cursor.execute(LOAD_PERIODIC_WITHOUT_FACTS_SQL, (company_id, since))
    return [(row[0], row[1], row[2]) for row in cursor.fetchall()]


INSERT_LINK_SQL = """
    INSERT INTO public.registrant_links (
        company_id, successor_cik, predecessor_cik, succession_form, succession_accession,
        succession_date, status, evidence, observed_at, run_id
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
    RETURNING id;
"""


def insert_registrant_link(cursor, *, company_id: int, link, evidence: Mapping, observed_at: datetime, run_id: int) -> bool:
    cursor.execute(INSERT_LINK_SQL, (
        company_id, link.successor_cik, link.predecessor_cik, link.succession_form,
        link.succession_accession, link.succession_date, link.status, _jsonb(dict(evidence)),
        observed_at, run_id,
    ))
    return cursor.fetchone() is not None


def load_registrant_links(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None):
    from database.registrant_v3 import RegistrantLink

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT DISTINCT successor_cik, predecessor_cik, succession_form, succession_accession,
                      succession_date, status
               FROM public.registrant_links WHERE company_id = %s AND observed_at <= %s;""",
            (company_id, as_of),
        )
        return [RegistrantLink(*row) for row in cursor.fetchall()]


def load_registrant_filings(cursor, company_id: int, cik: str) -> list[tuple[str, date, str]]:
    cursor.execute(
        "SELECT form, filing_date, accession FROM public.sec_filings WHERE company_id = %s AND cik = %s;",
        (company_id, cik),
    )
    return [(row[0], row[1], row[2]) for row in cursor.fetchall()]


def load_linked_predecessors(cursor, company_id: int) -> list[str]:
    cursor.execute(
        "SELECT DISTINCT predecessor_cik FROM public.registrant_links WHERE company_id = %s AND status = 'LINKED';",
        (company_id,),
    )
    return [row[0] for row in cursor.fetchall()]


def load_filing_forms(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None) -> list[tuple[str, date]]:
    """Official EDGAR filing metadata observed at ``as_of``: (form, filing_date)."""

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT form, filing_date FROM public.sec_filings WHERE company_id = %s AND observed_at <= %s;",
            (company_id, as_of),
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]


def load_company(ticker: str, *, connection_factory: Callable | None = None) -> tuple[int, str | None] | None:
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id, cik FROM public.companies WHERE ticker = %s;", (ticker.upper(),))
        rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"ticker {ticker} is not unique in companies")
    return rows[0] if rows else None


INSERT_ACCEPTANCE_SQL = """
    INSERT INTO public.sec_filing_acceptance_observations (company_id, accession, acceptance_at, observed_at, run_id)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (company_id, accession, acceptance_at) DO NOTHING
    RETURNING id;
"""


def insert_acceptance_observations(
    cursor, *, company_id: int, filings: Iterable[FilingRecord], observed_at: datetime, run_id: int,
) -> int:
    """Store each distinct acceptance time once; returns how many were new."""

    inserted = 0
    for filing in filings:
        if filing.acceptance_at is None:
            continue
        cursor.execute(INSERT_ACCEPTANCE_SQL, (company_id, filing.accession, filing.acceptance_at, observed_at, run_id))
        if cursor.fetchone() is not None:
            inserted += 1
    return inserted


INSERT_FILING_ITEM_SQL = """
    INSERT INTO public.sec_filing_items (company_id, accession, item, observed_at, run_id)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (company_id, accession, item) DO NOTHING
    RETURNING id;
"""


def insert_filing_items(
    cursor, *, company_id: int, filings: Iterable[FilingRecord], observed_at: datetime, run_id: int,
) -> int:
    """Store each filing's literal 8-K items once; returns how many were new."""

    inserted = 0
    for filing in filings:
        for item in dict.fromkeys(filing.items):
            cursor.execute(INSERT_FILING_ITEM_SQL, (company_id, filing.accession, item, observed_at, run_id))
            if cursor.fetchone() is not None:
                inserted += 1
    return inserted


LOAD_CATALYST_FILINGS_SQL = """
    SELECT f.cik, f.accession, f.form, f.filing_date, a.latest, a.distinct_values, i.item
    FROM public.sec_filing_items AS i
    JOIN public.sec_filings AS f ON f.company_id = i.company_id AND f.accession = i.accession
    CROSS JOIN LATERAL (
        SELECT MAX(o.acceptance_at) AS latest, COUNT(*) AS distinct_values
        FROM public.sec_filing_acceptance_observations AS o
        WHERE o.company_id = f.company_id AND o.accession = f.accession AND o.observed_at <= %(as_of)s
    ) AS a
    WHERE i.company_id = %(company_id)s AND i.observed_at <= %(as_of)s AND f.observed_at <= %(as_of)s
      AND f.form IN ('8-K', '8-K/A') AND f.filing_date >= %(since)s
    ORDER BY f.filing_date, f.accession, i.item;
"""


def load_catalyst_filings(company_id: int, as_of: datetime, since: date, *,
                          connection_factory: Callable | None = None) -> list:
    from database.catalysts_v3 import CatalystFiling

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_CATALYST_FILINGS_SQL, {"company_id": company_id, "as_of": as_of, "since": since})
        rows = cursor.fetchall()
    grouped: dict[str, list] = {}
    for cik, accession, form, filing_date, acceptance_at, values, item in rows:
        grouped.setdefault(accession, [cik, form, filing_date, acceptance_at, values, []])[5].append(item)
    return [
        CatalystFiling(cik=cik, accession=accession, form=form, filing_date=filing_date,
                       acceptance_at=acceptance_at, items=tuple(items), acceptance_values=max(values, 1))
        for accession, (cik, form, filing_date, acceptance_at, values, items) in grouped.items()
    ]


LOAD_LATEST_PRICE_BARS_SQL = """
    SELECT DISTINCT ON (bar_date) bar_date, open, high, low, close, adj_close, volume, currency
    FROM public.yahoo_price_bars_raw
    WHERE company_id = %s
    ORDER BY bar_date, observed_at DESC, id DESC;
"""

INSERT_PRICE_BAR_SQL = """
    INSERT INTO public.yahoo_price_bars_raw (
        company_id, ticker, bar_date, open, high, low, close, adj_close, volume,
        currency, exchange_timezone, observed_at, run_id
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
"""


def _bar_values(bar) -> tuple:
    return (bar.open, bar.high, bar.low, bar.close, bar.adj_close, Decimal(bar.volume), bar.currency)


def insert_price_bars(
    cursor, *, company_id: int, ticker: str, bars: Sequence, exchange_timezone: str, run_id: int,
) -> tuple[int, int]:
    """Append bars whose values differ from their latest stored observation."""

    cursor.execute(LOAD_LATEST_PRICE_BARS_SQL, (company_id,))
    latest = {row[0]: tuple(Decimal(value) for value in row[1:7]) + (row[7],) for row in cursor.fetchall()}
    changed = [bar for bar in bars if latest.get(bar.bar_date) != _bar_values(bar)]
    rows = [
        (company_id, ticker, bar.bar_date, bar.open, bar.high, bar.low, bar.close, bar.adj_close,
         bar.volume, bar.currency, exchange_timezone, bar.observed_at, run_id)
        for bar in changed
    ]
    if rows:
        cursor.executemany(INSERT_PRICE_BAR_SQL, rows)
    return len(rows), len(bars) - len(rows)


LOAD_PRICE_BARS_SQL = """
    SELECT id, bar_date, open, high, low, close, adj_close, volume, currency, observed_at
    FROM public.yahoo_price_bars_raw
    WHERE company_id = %s AND observed_at <= %s AND bar_date <= %s
    ORDER BY bar_date, observed_at, id;
"""


def load_price_bars(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None) -> list:
    from database.prices_v3 import PriceBar

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_PRICE_BARS_SQL, (company_id, as_of, as_of.date()))
        return [
            PriceBar(
                bar_date=row[1], open=_plain_decimal(row[2]), high=_plain_decimal(row[3]),
                low=_plain_decimal(row[4]), close=_plain_decimal(row[5]), adj_close=_plain_decimal(row[6]),
                volume=int(row[7]), currency=row[8], observed_at=row[9], id=row[0],
            )
            for row in cursor.fetchall()
        ]
