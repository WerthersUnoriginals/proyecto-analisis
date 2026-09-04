"""Backfill transaccional de evidencia raw SEC a fundamentals_normalized."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.normalized_fundamentals import (
    FIND_NORMALIZED_SQL,
    INSERT_NORMALIZED_SQL,
    _assert_same_semantics,
    _observation_values,
    _original_sec_fiscal_identity,
    load_raw_fundamentals,
    normalize_sec_raw_row,
)


def get_connection():
    """Carga el driver PostgreSQL sólo cuando se necesita una escritura real."""

    from database.db import get_connection as connect

    return connect()


SUMMARY_KEYS = (
    "raw_read", "normalized_candidates", "inserted", "existing", "review", "rejected",
)


def _empty_summary():
    return dict.fromkeys(SUMMARY_KEYS, 0)


def _normalize_sec_rows(rows):
    """Conserva cada raw SEC; sólo comparte identidad fiscal por métrica-periodo."""

    groups = defaultdict(list)
    for row in rows:
        groups[row["period_end"]].append(row)
    fiscal_identities = {
        period_end: _original_sec_fiscal_identity(group) for period_end, group in groups.items()
    }

    observations = []
    review = 0
    for row in rows:
        observation = normalize_sec_raw_row(
            row, fiscal_identities[row["period_end"]],
        )
        observations.append(observation)
        if observation.intrinsic_quality_status != "OK":
            review += 1
    return observations, review, 0


def _insert_batch_in_transaction(cursor, observations):
    """Inserta append-only usando el cursor de la única transacción del backfill."""

    inserted = existing = 0
    for observation in observations:
        cursor.execute(INSERT_NORMALIZED_SQL, _observation_values(observation))
        row = cursor.fetchone()
        if row is not None:
            inserted += 1
            continue
        cursor.execute(
            FIND_NORMALIZED_SQL,
            (observation.raw_id, observation.normalizer_version),
        )
        stored = cursor.fetchone()
        if stored is None:
            raise RuntimeError("No se pudo insertar ni localizar el fundamental normalizado.")
        _assert_same_semantics(observation, stored[1:])
        existing += 1
    return inserted, existing


def backfill_company(company_id: int, source: str, dry_run: bool = True) -> dict:
    """Normaliza todos los raw SEC de una empresa, sin tocar la tabla legacy."""

    if source != "SEC":
        raise ValueError("Task 4 sólo admite la fuente SEC.")
    rows = load_raw_fundamentals(company_id, source="SEC")
    observations, review, rejected = _normalize_sec_rows(rows)
    summary = _empty_summary()
    summary.update(
        raw_read=len(rows),
        normalized_candidates=len(observations),
        review=review,
        rejected=rejected,
    )
    if dry_run:
        return summary

    # transaction() confirma una sola vez al salir con éxito y revierte todo el
    # lote si una inserción o validación de conflicto falla.
    with get_connection() as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                inserted, existing = _insert_batch_in_transaction(cursor, observations)
    summary.update(inserted=inserted, existing=existing)
    return summary


def _company_id_for_ticker(ticker: str) -> int:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE ticker = %s", (ticker,))
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"No existe la empresa {ticker} en PostgreSQL.")
    return row[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Ticker existente, por ejemplo AAPL")
    parser.add_argument("--source", required=True, choices=("SEC",))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    ticker = args.ticker.upper().strip()
    summary = backfill_company(
        _company_id_for_ticker(ticker), args.source, dry_run=args.dry_run,
    )
    print(json.dumps(summary, sort_keys=True))
    return summary


if __name__ == "__main__":
    main()
