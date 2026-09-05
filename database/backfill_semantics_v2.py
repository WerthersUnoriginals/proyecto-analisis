"""Migración y backfill atómicos de trazabilidad semántica normalizada v2."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.backfill_normalized import _insert_batch_in_transaction
from database.normalized_fundamentals import (
    SEC_NORMALIZER_VERSION,
    SEC_NORMALIZER_V2,
    YAHOO_NORMALIZER_V2,
    NormalizedObservation,
    _original_sec_fiscal_identity,
    normalize_sec_raw_row_v2,
    normalize_yahoo_raw_row_v2,
)

MIGRATION = Path(__file__).with_name("migrations") / "2026-09-04_fundamentals_semantics_v2.sql"


def get_connection():
    from database.db import get_connection as connect

    return connect()


RAW_SQL = """
SELECT id, company_id, source,
       CASE WHEN source = 'SEC' THEN 'sec.company_facts' END AS source_variant,
       metric, period_start, period_end, filed_date, fiscal_year, fiscal_quarter,
       form_type, value, unit, currency, xbrl_tag, source_record_id, source_payload,
       fetched_at, created_at
FROM fundamentals_raw
WHERE company_id = %s AND source = %s
ORDER BY period_end, metric, filed_date, id
"""

CALENDAR_SQL = """
SELECT id, company_id, metric, source, source_variant, observation_kind, value, unit,
       currency, source_period_start, source_period_end, canonical_period_end, series_date,
       fiscal_year, fiscal_quarter, filed_date, source_available_at, observed_at, raw_id,
       normalizer_version, intrinsic_quality_status, intrinsic_quality_reasons,
       selection_eligibility, alignment_method, alignment_days, alignment_reference_id
FROM fundamentals_normalized
WHERE company_id = %s AND normalizer_version = %s
ORDER BY source_period_end, metric, raw_id
"""


def _dict_rows(cursor):
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _load_raw(cursor, company_id, source):
    cursor.execute(RAW_SQL, (company_id, source))
    return _dict_rows(cursor)


def _load_sec_calendar(cursor, company_id):
    cursor.execute(CALENDAR_SQL, (company_id, SEC_NORMALIZER_VERSION))
    rows = _dict_rows(cursor)
    for row in rows:
        row["intrinsic_quality_reasons"] = tuple(row["intrinsic_quality_reasons"])
    return [NormalizedObservation(**row) for row in rows]


def build_v2_candidates(sec_rows, yahoo_rows, sec_calendar=()):
    """Construye exactamente una observación v2 por raw, sin consultar proveedores."""

    groups = defaultdict(list)
    for row in sec_rows:
        groups[row["period_end"]].append(row)
    identities = {
        period_end: _original_sec_fiscal_identity(rows)
        for period_end, rows in groups.items()
    }
    sec_v2 = [
        normalize_sec_raw_row_v2(row, identities[row["period_end"]])
        for row in sec_rows
    ]
    calendar = list(sec_calendar) or sec_v2
    yahoo_v2 = []
    for row in yahoo_rows:
        payload = row.get("source_payload") or {}
        variant = payload.get("source_variant")
        if not variant:
            raise ValueError(f"Yahoo raw {row['id']} no conserva source_variant")
        yahoo_v2.append(normalize_yahoo_raw_row_v2(row, variant, calendar))
    return sec_v2 + yahoo_v2


def _summary(candidates, inserted=0, existing=0):
    counts = Counter((item.source, item.metric, item.selection_eligibility) for item in candidates)
    return {
        "candidates": len(candidates),
        "sec_v2": sum(1 for item in candidates if item.source == "SEC"),
        "yahoo_v2": sum(1 for item in candidates if item.source == "YAHOO"),
        "yahoo_eligible": sum(
            1 for item in candidates
            if item.source == "YAHOO" and item.selection_eligibility == "ELIGIBLE"
        ),
        "yahoo_eps_unspecified": sum(
            1 for item in candidates
            if item.source == "YAHOO" and item.metric == "EPS_UNSPECIFIED"
        ),
        "inserted": inserted,
        "existing": existing,
        "distribution": {"|".join(key): value for key, value in sorted(counts.items())},
    }


def _database_gates(cursor, company_id, v1_ids):
    cursor.execute("""
        SELECT
          count(*) FILTER (WHERE source='SEC'),
          count(*) FILTER (WHERE source='YAHOO'),
          count(*) FILTER (WHERE normalizer_version IN ('sec-normalized-v1','yahoo-normalized-v1')),
          count(*) FILTER (WHERE normalizer_version IN ('sec-normalized-v2','yahoo-normalized-v2')),
          count(*) FILTER (WHERE normalizer_version='sec-normalized-v2'),
          count(*) FILTER (WHERE normalizer_version='yahoo-normalized-v2'),
          count(*) FILTER (WHERE normalizer_version='yahoo-normalized-v2' AND selection_eligibility='ELIGIBLE'),
          count(*) FILTER (WHERE normalizer_version='yahoo-normalized-v2' AND metric='EPS_UNSPECIFIED'),
          count(*) FILTER (WHERE observation_kind='DERIVED')
        FROM fundamentals_normalized WHERE company_id=%s
    """, (company_id,))
    _, _, v1, v2, sec_v2, yahoo_v2, yahoo_eligible, unspecified, derived = cursor.fetchone()
    cursor.execute("SELECT array_agg(id ORDER BY id) FROM fundamentals_normalized WHERE company_id=%s AND normalizer_version IN ('sec-normalized-v1','yahoo-normalized-v1')", (company_id,))
    if tuple(cursor.fetchone()[0] or ()) != v1_ids:
        raise RuntimeError("Gate fallido: las identidades v1 cambiaron")
    cursor.execute("SELECT source, count(*) FROM fundamentals_raw WHERE company_id=%s GROUP BY source", (company_id,))
    raw = dict(cursor.fetchall())
    cursor.execute("SELECT count(*) FROM fundamentals_quarterly WHERE company_id=%s", (company_id,))
    quarterly = cursor.fetchone()[0]
    cursor.execute("""
        SELECT metric, count(DISTINCT (fiscal_year, fiscal_quarter))
        FROM fundamentals_normalized
        WHERE company_id=%s AND normalizer_version='sec-normalized-v2'
          AND selection_eligibility='ELIGIBLE'
        GROUP BY metric
    """, (company_id,))
    effective = dict(cursor.fetchall())
    cursor.execute("""
        SELECT count(DISTINCT metric)
        FROM fundamentals_normalized
        WHERE company_id=%s AND normalizer_version='yahoo-normalized-v2'
          AND fiscal_year=2025 AND fiscal_quarter=4
          AND selection_eligibility='ELIGIBLE'
          AND metric IN ('EPS_DILUTED','REVENUE','NET_INCOME')
    """, (company_id,))
    q4 = cursor.fetchone()[0]
    expected_effective = {"EPS_DILUTED": 19, "REVENUE": 19, "NET_INCOME": 19, "DILUTED_SHARES": 18}
    actual = {
        "raw_sec": raw.get("SEC", 0), "raw_yahoo": raw.get("YAHOO", 0),
        "quarterly": quarterly, "v1": v1, "v2": v2, "sec_v2": sec_v2,
        "yahoo_v2": yahoo_v2, "yahoo_eligible": yahoo_eligible,
        "yahoo_eps_unspecified": unspecified, "derived": derived,
        "sec_effective": effective, "q4_metrics": q4,
    }
    expected = {
        "raw_sec": 135, "raw_yahoo": 30, "quarterly": 19, "v1": 165,
        "v2": 165, "sec_v2": 135, "yahoo_v2": 30, "yahoo_eligible": 25,
        "yahoo_eps_unspecified": 5, "derived": 0,
        "sec_effective": expected_effective, "q4_metrics": 3,
    }
    if actual != expected:
        raise RuntimeError(f"Gates 6B fallidos: esperado={expected!r}, actual={actual!r}")
    return actual


def run(company_id: int, dry_run: bool):
    with get_connection() as connection:
        if dry_run:
            with connection.cursor() as cursor:
                candidates = build_v2_candidates(
                    _load_raw(cursor, company_id, "SEC"),
                    _load_raw(cursor, company_id, "YAHOO"),
                    _load_sec_calendar(cursor, company_id),
                )
            return _summary(candidates)

        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SELECT array_agg(id ORDER BY id) FROM fundamentals_normalized WHERE company_id=%s AND normalizer_version IN ('sec-normalized-v1','yahoo-normalized-v1')", (company_id,))
                v1_ids = tuple(cursor.fetchone()[0] or ())
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))
                candidates = build_v2_candidates(
                    _load_raw(cursor, company_id, "SEC"),
                    _load_raw(cursor, company_id, "YAHOO"),
                    _load_sec_calendar(cursor, company_id),
                )
                inserted, existing = _insert_batch_in_transaction(cursor, candidates)
                gates = _database_gates(cursor, company_id, v1_ids)
        result = _summary(candidates, inserted, existing)
        result["gates"] = gates
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE ticker=%s", (args.ticker.upper(),))
            found = cursor.fetchone()
    if not found:
        raise RuntimeError(f"No existe {args.ticker.upper()}")
    result = run(found[0], dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True, default=str))
    return result


if __name__ == "__main__":
    main()
