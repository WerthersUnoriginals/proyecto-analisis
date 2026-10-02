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
        catalog_version, observed_at, run_id
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            fact.filed_date, fact.frame, CATALOG_VERSION, observed_at, run_id,
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
    SELECT form, filing_date, acceptance_at FROM public.sec_filings
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
        if row is None or (row[0], row[1], row[2]) != (filing.form, filing.filing_date, filing.acceptance_at):
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
           fiscal_year, fiscal_period, form, filed_date, frame, observed_at
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
                filed_date=row[11], frame=row[12], observed_at=row[13], id=row[0],
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


def load_company(ticker: str, *, connection_factory: Callable | None = None) -> tuple[int, str | None] | None:
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id, cik FROM public.companies WHERE ticker = %s;", (ticker.upper(),))
        rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"ticker {ticker} is not unique in companies")
    return rows[0] if rows else None
