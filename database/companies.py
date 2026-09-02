from database.db import get_connection


def upsert_company(
    ticker,
    company_name,
    exchange,
    cik=None,
    country=None,
    currency=None,
    sector=None,
    industry=None,
):
    """
    Crea una empresa si no existe.
    Si ya existe el mismo ticker + exchange, actualiza sus datos.
    """

    sql = """
        INSERT INTO companies (
            ticker,
            company_name,
            cik,
            exchange,
            country,
            currency,
            sector,
            industry
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)

        ON CONFLICT (ticker, exchange)
        DO UPDATE SET
            company_name = EXCLUDED.company_name,
            cik = EXCLUDED.cik,
            country = EXCLUDED.country,
            currency = EXCLUDED.currency,
            sector = EXCLUDED.sector,
            industry = EXCLUDED.industry,
            is_active = TRUE,
            updated_at = NOW()

        RETURNING id, ticker, company_name;
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    ticker,
                    company_name,
                    cik,
                    exchange,
                    country,
                    currency,
                    sector,
                    industry,
                ),
            )

            return cur.fetchone()