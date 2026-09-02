from datetime import date

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

LATEST_SEC_RAW_SQL = """
    WITH ranked AS (
        SELECT
            id,
            company_id,
            metric,
            period_start,
            period_end,
            filed_date,
            fiscal_year,
            fiscal_quarter,
            form_type,
            value,
            xbrl_tag,
            ROW_NUMBER() OVER (
                PARTITION BY metric, period_end
                ORDER BY filed_date DESC NULLS LAST, id DESC
            ) AS rn
        FROM fundamentals_raw
        WHERE company_id = %s
          AND source = 'SEC'
    )
    SELECT
        id,
        metric,
        period_start,
        period_end,
        filed_date,
        fiscal_year,
        fiscal_quarter,
        form_type,
        value,
        xbrl_tag
    FROM ranked
    WHERE rn = 1
    ORDER BY period_end, metric;
"""

UPSERT_QUARTERLY_SQL = """
    INSERT INTO fundamentals_quarterly (
        company_id,
        fiscal_year,
        fiscal_quarter,
        period_start,
        period_end,
        latest_filed_date,
        form_type,
        eps_diluted,
        revenue,
        net_income,
        diluted_shares,
        eps_source,
        revenue_source,
        net_income_source,
        diluted_shares_source,
        eps_xbrl_tag,
        revenue_xbrl_tag,
        net_income_xbrl_tag,
        diluted_shares_xbrl_tag,
        eps_raw_id,
        revenue_raw_id,
        net_income_raw_id,
        diluted_shares_raw_id,
        shares_quality,
        data_quality,
        data_integrity,
        normalized_by_version
    )
    VALUES (
        %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s
    )
    ON CONFLICT (company_id, period_end)
    DO UPDATE SET
        fiscal_year = EXCLUDED.fiscal_year,
        fiscal_quarter = EXCLUDED.fiscal_quarter,
        period_start = EXCLUDED.period_start,
        latest_filed_date = EXCLUDED.latest_filed_date,
        form_type = EXCLUDED.form_type,
        eps_diluted = EXCLUDED.eps_diluted,
        revenue = EXCLUDED.revenue,
        net_income = EXCLUDED.net_income,
        diluted_shares = EXCLUDED.diluted_shares,
        eps_source = EXCLUDED.eps_source,
        revenue_source = EXCLUDED.revenue_source,
        net_income_source = EXCLUDED.net_income_source,
        diluted_shares_source = EXCLUDED.diluted_shares_source,
        eps_xbrl_tag = EXCLUDED.eps_xbrl_tag,
        revenue_xbrl_tag = EXCLUDED.revenue_xbrl_tag,
        net_income_xbrl_tag = EXCLUDED.net_income_xbrl_tag,
        diluted_shares_xbrl_tag = EXCLUDED.diluted_shares_xbrl_tag,
        eps_raw_id = EXCLUDED.eps_raw_id,
        revenue_raw_id = EXCLUDED.revenue_raw_id,
        net_income_raw_id = EXCLUDED.net_income_raw_id,
        diluted_shares_raw_id = EXCLUDED.diluted_shares_raw_id,
        shares_quality = EXCLUDED.shares_quality,
        data_quality = EXCLUDED.data_quality,
        data_integrity = EXCLUDED.data_integrity,
        normalized_by_version = EXCLUDED.normalized_by_version,
        updated_at = NOW()
    RETURNING id;
"""

NORMALIZER_VERSION = "sec-quarterly-v1"


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


def _shares_quality_from_tag(tag):
    if not tag:
        return "NOT_AVAILABLE"
    text = str(tag).lower()
    if "basicanddiluted" in text or ("basic" in text and "diluted" in text):
        return "BASIC_AND_DILUTED"
    if "diluted" in text:
        return "DILUTED_EXACT"
    if "basic" in text:
        return "BASIC_FALLBACK"
    return "REVIEW_REQUIRED"


def _latest_non_null(rows, key):
    ordered = sorted(
        rows,
        key=lambda row: (row.get("filed_date") or date.min, row["id"]),
        reverse=True,
    )
    for row in ordered:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _quarterly_record(company_id, period_end, metric_rows):
    rows = list(metric_rows.values())
    latest_filed_date = max(
        (row["filed_date"] for row in rows if row["filed_date"] is not None),
        default=None,
    )

    eps = metric_rows.get("EPS_DILUTED")
    revenue = metric_rows.get("REVENUE")
    net_income = metric_rows.get("NET_INCOME")
    shares = metric_rows.get("DILUTED_SHARES")

    core_count = sum(item is not None for item in (eps, revenue, net_income))
    if core_count == 3:
        data_quality = "core_complete"
    elif core_count >= 1:
        data_quality = "core_partial"
    else:
        data_quality = "insufficient"

    shares_quality = _shares_quality_from_tag(shares.get("xbrl_tag") if shares else None)

    return {
        "company_id": company_id,
        "fiscal_year": _latest_non_null(rows, "fiscal_year"),
        "fiscal_quarter": _latest_non_null(rows, "fiscal_quarter"),
        "period_start": _latest_non_null(rows, "period_start"),
        "period_end": period_end,
        "latest_filed_date": latest_filed_date,
        "form_type": _latest_non_null(rows, "form_type"),
        "eps_diluted": eps["value"] if eps else None,
        "revenue": revenue["value"] if revenue else None,
        "net_income": net_income["value"] if net_income else None,
        "diluted_shares": shares["value"] if shares else None,
        "eps_source": "SEC" if eps else None,
        "revenue_source": "SEC" if revenue else None,
        "net_income_source": "SEC" if net_income else None,
        "diluted_shares_source": "SEC" if shares else None,
        "eps_xbrl_tag": eps["xbrl_tag"] if eps else None,
        "revenue_xbrl_tag": revenue["xbrl_tag"] if revenue else None,
        "net_income_xbrl_tag": net_income["xbrl_tag"] if net_income else None,
        "diluted_shares_xbrl_tag": shares["xbrl_tag"] if shares else None,
        "eps_raw_id": eps["id"] if eps else None,
        "revenue_raw_id": revenue["id"] if revenue else None,
        "net_income_raw_id": net_income["id"] if net_income else None,
        "diluted_shares_raw_id": shares["id"] if shares else None,
        "shares_quality": shares_quality,
        "data_quality": data_quality,
        "data_integrity": None,
        "normalized_by_version": NORMALIZER_VERSION,
    }


def normalize_sec_quarterly(company_id):
    """
    Normaliza fundamentals_raw de SEC a una fila por company_id + period_end.

    Para cada métrica y period_end elige el hecho con filed_date más reciente;
    si hay empate, usa el id más alto. Mantiene trazabilidad mediante *_raw_id.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(LATEST_SEC_RAW_SQL, (company_id,))
            columns = [desc.name for desc in cur.description]
            raw_rows = [dict(zip(columns, row)) for row in cur.fetchall()]

            periods = {}
            for row in raw_rows:
                periods.setdefault(row["period_end"], {})[row["metric"]] = row

            normalized_ids = []
            for period_end in sorted(periods):
                record = _quarterly_record(company_id, period_end, periods[period_end])
                cur.execute(
                    UPSERT_QUARTERLY_SQL,
                    (
                        record["company_id"],
                        record["fiscal_year"],
                        record["fiscal_quarter"],
                        record["period_start"],
                        record["period_end"],
                        record["latest_filed_date"],
                        record["form_type"],
                        record["eps_diluted"],
                        record["revenue"],
                        record["net_income"],
                        record["diluted_shares"],
                        record["eps_source"],
                        record["revenue_source"],
                        record["net_income_source"],
                        record["diluted_shares_source"],
                        record["eps_xbrl_tag"],
                        record["revenue_xbrl_tag"],
                        record["net_income_xbrl_tag"],
                        record["diluted_shares_xbrl_tag"],
                        record["eps_raw_id"],
                        record["revenue_raw_id"],
                        record["net_income_raw_id"],
                        record["diluted_shares_raw_id"],
                        record["shares_quality"],
                        record["data_quality"],
                        record["data_integrity"],
                        record["normalized_by_version"],
                    ),
                )
                normalized_ids.append(cur.fetchone()[0])

    return {
        "company_id": company_id,
        "raw_metric_periods": len(raw_rows),
        "quarters_normalized": len(normalized_ids),
        "quarterly_ids": normalized_ids,
        "normalizer_version": NORMALIZER_VERSION,
    }
