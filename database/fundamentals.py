from psycopg.types.json import Jsonb

from database.db import get_connection


INSERT_RAW_SQL = """
    INSERT INTO fundamentals_raw (
        company_id,
        source,
        metric,
        period_start,
        period_end,
        filed_date,
        fiscal_year,
        fiscal_quarter,
        form_type,
        value,
        unit,
        currency,
        xbrl_tag,
        source_record_id,
        source_payload
    )
    VALUES (
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s
    )
    ON CONFLICT DO NOTHING
    RETURNING id;
"""

FIND_RAW_SQL = """
    SELECT id
    FROM fundamentals_raw
    WHERE company_id = %s
      AND source = %s
      AND metric = %s
      AND period_end = %s
      AND COALESCE(xbrl_tag, '') = COALESCE(%s, '')
      AND COALESCE(filed_date, DATE '0001-01-01')
          = COALESCE(%s, DATE '0001-01-01')
      AND COALESCE(source_record_id, '') = COALESCE(%s, '')
    LIMIT 1;
"""


def _payload_value(source_payload):
    return None if source_payload is None else Jsonb(source_payload)


def _insert_raw_with_cursor(cur, fact):
    cur.execute(
        INSERT_RAW_SQL,
        (
            fact["company_id"],
            fact["source"],
            fact["metric"],
            fact.get("period_start"),
            fact["period_end"],
            fact.get("filed_date"),
            fact.get("fiscal_year"),
            fact.get("fiscal_quarter"),
            fact.get("form_type"),
            fact["value"],
            fact.get("unit"),
            fact.get("currency"),
            fact.get("xbrl_tag"),
            fact.get("source_record_id"),
            _payload_value(fact.get("source_payload")),
        ),
    )

    row = cur.fetchone()
    if row is not None:
        return row[0]

    cur.execute(
        FIND_RAW_SQL,
        (
            fact["company_id"],
            fact["source"],
            fact["metric"],
            fact["period_end"],
            fact.get("xbrl_tag"),
            fact.get("filed_date"),
            fact.get("source_record_id"),
        ),
    )
    existing = cur.fetchone()
    if existing is None:
        raise RuntimeError("No se pudo insertar ni localizar el fundamental bruto.")
    return existing[0]


def insert_raw_fundamental(
    company_id,
    source,
    metric,
    period_end,
    value,
    period_start=None,
    filed_date=None,
    fiscal_year=None,
    fiscal_quarter=None,
    form_type=None,
    unit=None,
    currency=None,
    xbrl_tag=None,
    source_record_id=None,
    source_payload=None,
):
    """Guarda un hecho fundamental bruto y devuelve su id."""

    fact = {
        "company_id": company_id,
        "source": source,
        "metric": metric,
        "period_start": period_start,
        "period_end": period_end,
        "filed_date": filed_date,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "form_type": form_type,
        "value": value,
        "unit": unit,
        "currency": currency,
        "xbrl_tag": xbrl_tag,
        "source_record_id": source_record_id,
        "source_payload": source_payload,
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            return _insert_raw_with_cursor(cur, fact)


def insert_raw_fundamentals_batch(facts):
    """Guarda una colección de hechos en una sola transacción."""

    facts = list(facts)
    if not facts:
        return []

    ids = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for fact in facts:
                ids.append(_insert_raw_with_cursor(cur, fact))
    return ids
