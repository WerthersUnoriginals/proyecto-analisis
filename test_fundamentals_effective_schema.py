"""Structural contract tests for the append-only normalized fundamentals DDL.

Task 2 creates a schema artifact but does not authorize database application.
The tests therefore parse the complete DDL contract and mutation-test that
parser; PostgreSQL constraint execution is a later controlled-release gate.
"""

from pathlib import Path
import re
import unittest


SCHEMA_PATH = Path(__file__).resolve().parent / "database" / "schema_fundamentals_effective.sql"
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "database"
    / "migrations"
    / "2026-09-20_fundamentals_effective_current_v2.sql"
)

EFFECTIVE_CURRENT_COLUMNS = (
    "company_id",
    "metric",
    "fiscal_year",
    "fiscal_quarter",
    "canonical_period_end",
    "series_date",
    "value",
    "unit",
    "currency",
    "source",
    "source_variant",
    "observation_kind",
    "selected_observation_id",
    "raw_id",
    "source_metric_name",
    "source_unit",
    "source_scale_factor",
    "source_period_start",
    "source_period_end",
    "filed_date",
    "source_available_at",
    "observed_at",
    "normalizer_version",
    "intrinsic_quality_status",
    "selection_eligibility",
    "selection_policy_version",
    "selection_reason",
    "comparison_rules_version",
    "comparison_status",
    "comparison_reason",
    "comparison_reference_id",
    "comparison_difference_pct",
    "alignment_method",
    "alignment_days",
    "alignment_reference_id",
)

EXPECTED_COLUMNS = [
    ("id", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"),
    ("company_id", "BIGINT NOT NULL REFERENCES companies(id) ON DELETE RESTRICT"),
    ("metric", "TEXT NOT NULL"),
    ("source", "TEXT NOT NULL"),
    ("source_variant", "TEXT NOT NULL"),
    ("observation_kind", "TEXT NOT NULL"),
    ("value", "NUMERIC(30, 8) NOT NULL"),
    ("unit", "TEXT NOT NULL"),
    ("currency", "TEXT"),
    ("source_period_start", "DATE"),
    ("source_period_end", "DATE NOT NULL"),
    ("canonical_period_end", "DATE"),
    ("series_date", "DATE NOT NULL"),
    ("fiscal_year", "INTEGER"),
    ("fiscal_quarter", "SMALLINT"),
    ("filed_date", "DATE"),
    ("source_available_at", "TIMESTAMPTZ"),
    ("observed_at", "TIMESTAMPTZ NOT NULL"),
    ("raw_id", "BIGINT NOT NULL REFERENCES fundamentals_raw(id) ON DELETE RESTRICT"),
    ("normalizer_version", "TEXT NOT NULL"),
    ("intrinsic_quality_status", "TEXT NOT NULL"),
    ("intrinsic_quality_reasons", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
    ("selection_eligibility", "TEXT NOT NULL"),
    ("alignment_method", "TEXT NOT NULL"),
    ("alignment_days", "SMALLINT"),
    ("alignment_reference_id", "BIGINT REFERENCES fundamentals_normalized(id) ON DELETE RESTRICT"),
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]

EXPECTED_CONSTRAINTS = {
    "fundamentals_normalized_raw_version_unique": "UNIQUE (raw_id, normalizer_version)",
    "fundamentals_normalized_metric_check": "CHECK (metric IN ('EPS_DILUTED', 'REVENUE', 'NET_INCOME', 'DILUTED_SHARES'))",
    "fundamentals_normalized_unit_check": "CHECK ((metric = 'EPS_DILUTED' AND unit = 'USD/shares') OR (metric IN ('REVENUE', 'NET_INCOME') AND unit = 'USD') OR (metric = 'DILUTED_SHARES' AND unit = 'shares'))",
    "fundamentals_normalized_source_check": "CHECK (source IN ('SEC', 'YAHOO', 'DERIVED'))",
    "fundamentals_normalized_variant_check": "CHECK ((source = 'SEC' AND source_variant LIKE 'sec.%') OR (source = 'YAHOO' AND (source_variant LIKE 'yahoo.%' OR source_variant LIKE 'yfinance.%')) OR (source = 'DERIVED' AND source_variant LIKE 'derived.%'))",
    "fundamentals_normalized_observation_kind_check": "CHECK (observation_kind IN ('REPORTED', 'DERIVED'))",
    "fundamentals_normalized_source_kind_filing_check": "CHECK ((source = 'SEC' AND observation_kind = 'REPORTED' AND (selection_eligibility = 'INELIGIBLE' OR filed_date IS NOT NULL)) OR (source = 'YAHOO' AND observation_kind = 'REPORTED' AND filed_date IS NULL) OR (source = 'DERIVED' AND observation_kind = 'DERIVED' AND selection_eligibility = 'INELIGIBLE'))",
    "fundamentals_normalized_fiscal_quarter_check": "CHECK (fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4)",
    "fundamentals_normalized_period_check": "CHECK (source_period_start IS NULL OR source_period_start <= source_period_end)",
    "fundamentals_normalized_series_date_check": "CHECK (series_date = source_period_end)",
    "fundamentals_normalized_quality_status_check": "CHECK (intrinsic_quality_status IN ('OK', 'REVIEW_REQUIRED', 'REVIEW_REQUIRED_HIGH', 'REJECTED'))",
    "fundamentals_normalized_selection_eligibility_check": "CHECK (selection_eligibility IN ('ELIGIBLE', 'INELIGIBLE'))",
    "fundamentals_normalized_review_ineligible_check": "CHECK (intrinsic_quality_status = 'OK' OR (intrinsic_quality_status = 'REVIEW_REQUIRED' AND selection_eligibility = 'INELIGIBLE') OR (intrinsic_quality_status = 'REVIEW_REQUIRED_HIGH' AND selection_eligibility = 'INELIGIBLE') OR (intrinsic_quality_status = 'REJECTED' AND selection_eligibility = 'INELIGIBLE'))",
    "fundamentals_normalized_alignment_method_check": "CHECK (alignment_method IN ('EXACT', 'FISCAL_METADATA', 'SEC_CALENDAR', 'NEAREST_35D', 'SOURCE_ONLY', 'UNRESOLVED'))",
    "fundamentals_normalized_alignment_days_check": "CHECK ((canonical_period_end IS NULL AND alignment_days IS NULL) OR (canonical_period_end IS NOT NULL AND alignment_days IS NOT NULL AND alignment_days = source_period_end - canonical_period_end))",
    "fundamentals_normalized_exact_alignment_check": "CHECK ((alignment_method = 'EXACT' AND canonical_period_end IS NOT NULL AND alignment_days = 0) OR alignment_method IN ('FISCAL_METADATA', 'SEC_CALENDAR', 'NEAREST_35D', 'SOURCE_ONLY', 'UNRESOLVED'))",
    "fundamentals_normalized_unresolved_alignment_check": "CHECK ((alignment_method = 'UNRESOLVED' AND canonical_period_end IS NULL AND intrinsic_quality_status IN ('REVIEW_REQUIRED', 'REVIEW_REQUIRED_HIGH') AND selection_eligibility = 'INELIGIBLE') OR alignment_method IN ('EXACT', 'FISCAL_METADATA', 'SEC_CALENDAR', 'NEAREST_35D', 'SOURCE_ONLY'))",
}

EXPECTED_INDEXES = [
    "CREATE INDEX fundamentals_normalized_source_period_idx ON fundamentals_normalized (company_id, metric, source, source_variant, source_period_end)",
    "CREATE INDEX fundamentals_normalized_fiscal_idx ON fundamentals_normalized (company_id, metric, fiscal_year, fiscal_quarter)",
    "CREATE INDEX fundamentals_normalized_observed_idx ON fundamentals_normalized (company_id, metric, observed_at)",
]


def compact(text):
    normalized = re.sub(r"\s+", " ", text).strip()
    normalized = re.sub(r"\(\s+", "(", normalized)
    return re.sub(r"\s+\)", ")", normalized)


def normalized_table(sql):
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS fundamentals_normalized\s*\((?P<body>.*?)\n\);",
        sql,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("fundamentals_normalized table declaration is missing")
    return match.group("body")


def column_definitions(table):
    definitions = []
    for line in table.splitlines():
        match = re.match(r"^    ([a-z][a-z_]*)\s+(.+),$", line)
        if match:
            definitions.append((match.group(1), compact(match.group(2))))
    return definitions


def named_constraints(table):
    constraints = {}
    for match in re.finditer(
        r"(?ms)^    CONSTRAINT (?P<name>[a-z_]+)\s+(?P<expression>.*?)(?=,\n\n    CONSTRAINT|\n\);)",
        table + "\n);",
    ):
        constraints[match.group("name")] = compact(match.group("expression"))
    return constraints


def contract_errors(sql):
    table = normalized_table(sql)
    errors = []
    if column_definitions(table) != EXPECTED_COLUMNS:
        errors.append("column definitions differ from the 27-column contract")
    if named_constraints(table) != EXPECTED_CONSTRAINTS:
        errors.append("named constraints differ from the complete contract")
    return errors


class EffectiveSchemaTests(unittest.TestCase):
    """Protect the schema invariants required before any normalizer exists."""

    @classmethod
    def setUpClass(cls):
        cls.sql = SCHEMA_PATH.read_text(encoding="utf-8")
        cls.compact = compact(cls.sql)
        cls.table = normalized_table(cls.sql)

    def test_declares_exact_column_types_nullability_identity_and_defaults(self):
        """Catches a changed type, nullable required field, identity, or default."""
        self.assertEqual(EXPECTED_COLUMNS, column_definitions(self.table))
        self.assertNotIn("comparison_", self.sql.lower())

    def test_named_constraint_expressions_match_the_complete_contract(self):
        """Catches an omitted predicate or an additional permissive constraint branch."""
        self.assertEqual(EXPECTED_CONSTRAINTS, named_constraints(self.table))

    def test_alignment_requires_dates_and_signed_days_to_have_equivalent_nullability(self):
        """Catches canonical_period_end coexisting with NULL alignment_days."""
        self.assertEqual([], contract_errors(self.sql))
        self.assertEqual(
            "CHECK ((canonical_period_end IS NULL AND alignment_days IS NULL) OR (canonical_period_end IS NOT NULL AND alignment_days IS NOT NULL AND alignment_days = source_period_end - canonical_period_end))",
            named_constraints(self.table)["fundamentals_normalized_alignment_days_check"],
        )

    def test_mutation_checks_reject_weakened_source_kind_and_filing_rules(self):
        """Proves static review rejects permissive SEC, Yahoo, and DERIVED branches."""
        mutations = {
            "SEC eligible without filing": (
                "OR (source = 'YAHOO' AND observation_kind = 'REPORTED' AND filed_date IS NULL)",
                "OR (source = 'YAHOO' AND observation_kind = 'REPORTED' AND filed_date IS NULL)\n            OR (source = 'SEC' AND observation_kind = 'REPORTED'\n                AND selection_eligibility = 'ELIGIBLE' AND filed_date IS NULL)",
                "source = 'SEC' AND observation_kind = 'REPORTED' AND selection_eligibility = 'ELIGIBLE' AND filed_date IS NULL",
            ),
            "Yahoo accepts a filing": (
                "AND observation_kind = 'REPORTED' AND filed_date IS NULL",
                "AND observation_kind = 'REPORTED'",
                None,
            ),
            "DERIVED may claim reported": (
                "AND observation_kind = 'DERIVED'\n                AND selection_eligibility = 'INELIGIBLE')",
                "AND observation_kind = 'DERIVED'\n                AND selection_eligibility = 'INELIGIBLE')\n            OR (source = 'DERIVED' AND observation_kind = 'REPORTED')",
                None,
            ),
        }
        for label, (original, replacement, expected_permissive_branch) in mutations.items():
            with self.subTest(label=label):
                mutated = self.sql.replace(original, replacement, 1)
                self.assertNotEqual(self.sql, mutated)
                if expected_permissive_branch:
                    self.assertIn(expected_permissive_branch, compact(mutated))
                self.assertTrue(contract_errors(mutated))

    def test_mutation_checks_reject_missing_not_null_and_permissive_alignment(self):
        """Proves static review catches nullable observed data and absent signed days."""
        mutations = {
            "observed_at nullable": ("observed_at TIMESTAMPTZ NOT NULL", "observed_at TIMESTAMPTZ"),
            "canonical date permits null days": (
                "canonical_period_end IS NOT NULL\n                AND alignment_days IS NOT NULL\n                AND alignment_days = source_period_end - canonical_period_end",
                "canonical_period_end IS NOT NULL\n                AND alignment_days = source_period_end - canonical_period_end",
            ),
        }
        for label, (original, replacement) in mutations.items():
            with self.subTest(label=label):
                mutated = self.sql.replace(original, replacement, 1)
                self.assertNotEqual(self.sql, mutated)
                self.assertTrue(contract_errors(mutated))

    def test_declares_all_required_read_indexes_and_no_period_uniqueness(self):
        """Catches missing selection read paths or an invalid uniqueness by period."""
        for index in EXPECTED_INDEXES:
            self.assertIn(index, self.compact)
        self.assertNotRegex(
            self.compact,
            r"UNIQUE \(company_id,.*(?:source_period_end|canonical_period_end)",
        )

    def test_is_additive_and_excludes_effective_or_derived_processes(self):
        """Catches scope creep into legacy mutation, selection projection, or derivation."""
        lowered = self.sql.lower()
        for forbidden in (
            "drop ",
            "alter table",
            "fundamentals_quarterly",
            "create view",
            "create trigger",
            "effective_current",
            "fundamental_derivation_inputs",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)


class EffectiveCurrentMigrationTests(unittest.TestCase):
    """Static safety and interface contract for the unapplied Task 9A view."""

    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        cls.lowered = cls.sql.lower()
        cls.compact = compact(cls.sql)

    def test_creates_one_plain_view_without_mutation_or_materialization(self):
        self.assertEqual(
            len(re.findall(r"\bCREATE\s+VIEW\s+fundamentals_effective_current\b", self.sql, re.I)),
            1,
        )
        for forbidden in (
            "create table",
            "materialized view",
            "create trigger",
            "insert ",
            "update ",
            "delete ",
            "truncate ",
            "alter table",
            "drop ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.lowered)

    def test_pins_only_v2_normalizers_and_contract_versions(self):
        self.assertIn("'sec-normalized-v2'", self.sql)
        self.assertIn("'yahoo-normalized-v2'", self.sql)
        self.assertNotIn("normalized-v1", self.sql)
        self.assertNotRegex(self.sql, r"MAX\s*\(\s*normalizer_version", re.I)
        self.assertIn("'c-v2.6-compatible-v1'", self.sql)
        self.assertIn("'sec-yahoo-comparison-v1'", self.sql)

    def test_excludes_non_current_sources_metrics_and_kinds(self):
        for required in (
            "source IN ('SEC', 'YAHOO')",
            "observation_kind = 'REPORTED'",
            "selection_eligibility = 'ELIGIBLE'",
            "metric IN ('EPS_DILUTED', 'REVENUE', 'NET_INCOME', 'DILUTED_SHARES')",
            "normalized.source = 'YAHOO' AND normalized.metric = 'DILUTED_SHARES'",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.compact)

    def test_exposes_the_complete_minimum_contract(self):
        for column in EFFECTIVE_CURRENT_COLUMNS:
            with self.subTest(column=column):
                self.assertRegex(self.sql, rf"\bAS\s+{column}\b")
        for forbidden in ("yoy", "acceleration", "persistence", "trend_quality", "score"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.lowered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
