from database.db import get_connection


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
    """
    Guarda un hecho fundamental bruto.

    Si el mismo hecho ya existe segun la regla de deduplicacion
    de fundamentals_raw, devuelve el registro existente.
    """

    insert_sql = """
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

    find_sql = """
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                insert_sql,
                (
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
                    source_payload,
                ),
            )

            row = cur.fetchone()

            if row is not None:
                return row[0]

            cur.execute(
                find_sql,
                (
                    company_id,
                    source,
                    metric,
                    period_end,
                    xbrl_tag,
                    filed_date,
                    source_record_id,
                ),
            )

            existing = cur.fetchone()

            if existing is None:
                raise RuntimeError(
                    "No se pudo insertar ni localizar el fundamental bruto."
                )

            return existing[0]


def insert_raw_fundamentals_batch(facts):
    """
    Guarda una coleccion de hechos fundamentales brutos.

    Cada elemento de facts debe ser un diccionario compatible
    con insert_raw_fundamental().
    """

    ids = []

    for fact in facts:
        raw_id = insert_raw_fundamental(
            company_id=fact["company_id"],
            source=fact["source"],
            metric=fact["metric"],
            period_end=fact["period_end"],
            value=fact["value"],
            period_start=fact.get("period_start"),
            filed_date=fact.get("filed_date"),
            fiscal_year=fact.get("fiscal_year"),
            fiscal_quarter=fact.get("fiscal_quarter"),
            form_type=fact.get("form_type"),
            unit=fact.get("unit"),
            currency=fact.get("currency"),
            xbrl_tag=fact.get("xbrl_tag"),
            source_record_id=fact.get("source_record_id"),
            source_payload=fact.get("source_payload"),
        )
        ids.append(raw_id)

    return ids
