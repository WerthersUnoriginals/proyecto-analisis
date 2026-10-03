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


LATEST_PROFILE_SQL = """
    SELECT cik, sic, sic_description FROM public.company_profiles
    WHERE company_id = %s AND observed_at <= %s ORDER BY observed_at DESC, id DESC LIMIT 1;
"""


def insert_company_profile(cursor, *, company_id: int, cik: str, sic: str | None, sic_description: str | None,
                           observed_at: datetime, run_id: int) -> bool:
    """Append the SEC SIC classification when it differs from the latest one observed by then."""

    cursor.execute(LATEST_PROFILE_SQL, (company_id, observed_at))
    if cursor.fetchone() == (cik, sic, sic_description):
        return False
    cursor.execute(
        """INSERT INTO public.company_profiles (company_id, cik, sic, sic_description, observed_at, run_id)
           VALUES (%s, %s, %s, %s, %s, %s);""",
        (company_id, cik, sic, sic_description, observed_at, run_id),
    )
    return True


LOAD_PROFILES_SQL = """
    SELECT DISTINCT ON (company_id) company_id, sic, sic_description
    FROM public.company_profiles
    WHERE company_id = ANY(%s) AND observed_at <= %s
    ORDER BY company_id, observed_at DESC, id DESC;
"""


def load_profiles(company_ids, as_of: datetime, *, connection_factory: Callable | None = None) -> dict:
    """{company_id: (sic, sic_description)} as known at ``as_of``."""

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_PROFILES_SQL, (list(company_ids), as_of))
        return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}


def insert_universe_snapshot(cursor, *, company_id: int, universe: str, holdings_as_of: date, members,
                             observed_at: datetime, run_id: int) -> int | None:
    """Store one holdings file; None when that holdings date is already stored."""

    cursor.execute(
        """INSERT INTO public.universe_snapshots (company_id, universe, holdings_as_of, observed_at, run_id)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (universe, holdings_as_of) DO NOTHING RETURNING id;""",
        (company_id, universe, holdings_as_of, observed_at, run_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    cursor.executemany(
        """INSERT INTO public.universe_members (snapshot_id, position, source_ticker, name, identifier, sedol,
               weight, sector, shares_held, currency) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
        [(row[0], item.position, item.source_ticker, item.name, item.identifier, item.sedol, item.weight,
          item.sector, item.shares_held, item.currency) for item in members],
    )
    return row[0]


LOAD_UNIVERSE_SQL = """
    WITH snapshot AS (
        SELECT id, holdings_as_of FROM public.universe_snapshots
        WHERE universe = %s AND observed_at <= %s
        ORDER BY holdings_as_of DESC, observed_at DESC LIMIT 1
    )
    SELECT s.holdings_as_of, m.source_ticker
    FROM snapshot AS s JOIN public.universe_members AS m ON m.snapshot_id = s.id
    ORDER BY m.position;
"""


def load_universe(universe: str, as_of: datetime, *, connection_factory: Callable | None = None):
    """(holdings_as_of, [source tickers]) of the latest snapshot observed by ``as_of``."""

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_UNIVERSE_SQL, (universe, as_of))
        rows = cursor.fetchall()
    return (rows[0][0], [row[1] for row in rows]) if rows else (None, [])


def load_ticker_for_cik(cik: str, *, connection_factory: Callable | None = None) -> str | None:
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT ticker FROM public.companies WHERE cik = %s;", (cik,))
        row = cursor.fetchone()
    return row[0] if row else None


def load_company_ids(tickers, *, connection_factory: Callable | None = None) -> dict:
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT ticker, id FROM public.companies WHERE ticker = ANY(%s);", (list(tickers),))
        return dict(cursor.fetchall())


LOAD_MANY_PRICE_BARS_SQL = """
    SELECT company_id, id, bar_date, open, high, low, close, adj_close, volume, currency, observed_at
    FROM public.yahoo_price_bars_raw
    WHERE company_id = ANY(%s) AND observed_at <= %s AND bar_date <= %s
    ORDER BY company_id, bar_date, observed_at, id;
"""


def load_many_price_bars(company_ids, as_of: datetime, *, connection_factory: Callable | None = None) -> dict:
    """{company_id: [PriceBar]} visible at ``as_of`` for many companies in one query."""

    from database.prices_v3 import PriceBar

    result: dict = {company_id: [] for company_id in company_ids}
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_MANY_PRICE_BARS_SQL, (list(company_ids), as_of, as_of.date()))
        for row in cursor.fetchall():
            result[row[0]].append(PriceBar(
                bar_date=row[2], open=_plain_decimal(row[3]), high=_plain_decimal(row[4]), low=_plain_decimal(row[5]),
                close=_plain_decimal(row[6]), adj_close=_plain_decimal(row[7]), volume=int(row[8]),
                currency=row[9], observed_at=row[10], id=row[1],
            ))
    return result


def dataset_13f_stored(cursor, file_name: str, sha256: str) -> bool:
    cursor.execute("SELECT 1 FROM public.sec_13f_datasets WHERE file_name = %s AND sha256 = %s;", (file_name, sha256))
    return cursor.fetchone() is not None


def insert_13f_dataset(cursor, *, file_name: str, sha256: str, size_bytes: int, observed_at: datetime) -> int:
    cursor.execute(
        """INSERT INTO public.sec_13f_datasets (file_name, sha256, size_bytes, observed_at)
           VALUES (%s, %s, %s, %s) RETURNING id;""",
        (file_name, sha256, size_bytes, observed_at),
    )
    return cursor.fetchone()[0]


def insert_13f_filings(cursor, *, dataset_id: int, filings) -> int:
    """Store submissions not seen before (an accession is stored once)."""

    before = cursor.execute("SELECT COUNT(*) FROM public.sec_13f_filings;").fetchone()[0]
    cursor.executemany(
        """INSERT INTO public.sec_13f_filings (accession, dataset_id, filer_cik, filing_date, submission_type,
               period_of_report, amendment_type) VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (accession) DO NOTHING;""",
        [(item.accession, dataset_id, item.filer_cik, item.filing_date, item.submission_type, item.period_of_report,
          item.amendment_type) for item in filings],
    )
    return cursor.execute("SELECT COUNT(*) FROM public.sec_13f_filings;").fetchone()[0] - before


def insert_13f_holdings(cursor, *, dataset_id: int, holdings) -> int:
    """Bulk-load tracked rows through a temporary table; existing (accession, row) pairs are kept."""

    cursor.execute(
        """CREATE TEMP TABLE IF NOT EXISTS sec_13f_holdings_load (LIKE public.sec_13f_holdings) ON COMMIT DROP;"""
    )
    with cursor.copy(
        "COPY sec_13f_holdings_load (accession, infotable_sk, dataset_id, cusip, name_of_issuer, title_of_class, "
        "value, shares, shares_type, put_call) FROM STDIN"
    ) as copy:
        for item in holdings:
            copy.write_row((item.accession, item.infotable_sk, dataset_id, item.cusip, item.name_of_issuer,
                            item.title_of_class, item.value, item.shares, item.shares_type, item.put_call))
    cursor.execute(
        """INSERT INTO public.sec_13f_holdings SELECT l.* FROM sec_13f_holdings_load AS l
           WHERE EXISTS (SELECT 1 FROM public.sec_13f_filings AS f WHERE f.accession = l.accession)
           ON CONFLICT (accession, infotable_sk) DO NOTHING;"""
    )
    inserted = cursor.rowcount
    cursor.execute("DROP TABLE sec_13f_holdings_load;")
    return inserted


LATEST_CUSIP_SQL = """
    SELECT cusip, source FROM public.company_cusips
    WHERE company_id = %s AND observed_at <= %s ORDER BY observed_at DESC, id DESC LIMIT 1;
"""


def insert_company_cusip(cursor, *, company_id: int, cusip: str | None, source: str, evidence: Mapping,
                         observed_at: datetime) -> bool:
    cursor.execute(LATEST_CUSIP_SQL, (company_id, observed_at))
    if cursor.fetchone() == (cusip, source):
        return False
    cursor.execute(
        """INSERT INTO public.company_cusips (company_id, cusip, source, evidence, observed_at)
           VALUES (%s, %s, %s, %s, %s);""",
        (company_id, cusip, source, _jsonb(dict(evidence)), observed_at),
    )
    return True


def load_company_cusip(company_id: int, as_of: datetime, *, connection_factory: Callable | None = None):
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LATEST_CUSIP_SQL, (company_id, as_of))
        return cursor.fetchone() or (None, None)


LOAD_13F_FILINGS_SQL = """
    SELECT f.accession, f.filer_cik, f.filing_date, f.submission_type, f.period_of_report, f.amendment_type
    FROM public.sec_13f_filings AS f JOIN public.sec_13f_datasets AS d ON d.id = f.dataset_id
    WHERE f.period_of_report = ANY(%s) AND f.filing_date <= %s AND d.observed_at <= %s;
"""

LOAD_13F_HOLDINGS_SQL = """
    SELECT h.accession, h.infotable_sk, h.cusip, h.name_of_issuer, h.title_of_class, h.value, h.shares,
           h.shares_type, h.put_call
    FROM public.sec_13f_holdings AS h JOIN public.sec_13f_datasets AS d ON d.id = h.dataset_id
    WHERE h.cusip = %s AND d.observed_at <= %s;
"""


def load_13f(cusip: str, periods, as_of: datetime, *, connection_factory: Callable | None = None):
    """(filings of the periods, holdings of the CUSIP) visible at ``as_of``."""

    from database.sponsorship_v1 import Filing13F, Holding13F

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        cursor.execute(LOAD_13F_FILINGS_SQL, (list(periods), as_of.date(), as_of))
        filings = [Filing13F(*row) for row in cursor.fetchall()]
        cursor.execute(LOAD_13F_HOLDINGS_SQL, (cusip, as_of))
        holdings = [Holding13F(row[0], row[1], row[2], row[3], row[4], _plain_decimal(row[5]),
                               _plain_decimal(row[6]), row[7], row[8]) for row in cursor.fetchall()]
    return filings, holdings
