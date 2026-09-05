import re
import unittest
from pathlib import Path


MIGRATION = Path(__file__).parent / "database" / "migrations" / "2026-09-04_fundamentals_semantics_v2.sql"


class SemanticsV2MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.compact = re.sub(r"\s+", " ", cls.sql).strip().upper()

    def test_adds_the_three_normalized_traceability_columns(self):
        self.assertIn("ADD COLUMN IF NOT EXISTS SOURCE_METRIC_NAME TEXT", self.compact)
        self.assertIn("ADD COLUMN IF NOT EXISTS SOURCE_UNIT TEXT", self.compact)
        self.assertIn("ADD COLUMN IF NOT EXISTS SOURCE_SCALE_FACTOR NUMERIC", self.compact)

    def test_expands_raw_and_normalized_metric_domains(self):
        self.assertRegex(
            self.compact,
            r"FUNDAMENTALS_RAW_METRIC_CHECK.*EPS_BASIC",
        )
        self.assertRegex(
            self.compact,
            r"FUNDAMENTALS_NORMALIZED_METRIC_CHECK.*EPS_BASIC.*EPS_UNSPECIFIED",
        )

    def test_v2_requires_source_unit_metric_name_and_positive_scale(self):
        for name in (
            "FUNDAMENTALS_NORMALIZED_V2_SOURCE_METRIC_NAME_CHECK",
            "FUNDAMENTALS_NORMALIZED_V2_SOURCE_UNIT_CHECK",
            "FUNDAMENTALS_NORMALIZED_V2_SOURCE_SCALE_CHECK",
        ):
            self.assertIn(name, self.compact)
        self.assertIn("SOURCE_SCALE_FACTOR > 0", self.compact)
        self.assertIn("SEC-NORMALIZED-V2", self.compact)
        self.assertIn("YAHOO-NORMALIZED-V2", self.compact)

    def test_eps_unspecified_is_only_reviewable_ineligible_legacy_yahoo(self):
        self.assertIn("FUNDAMENTALS_NORMALIZED_EPS_UNSPECIFIED_CHECK", self.compact)
        for required in (
            "SOURCE = 'YAHOO'",
            "SOURCE_METRIC_NAME = 'LEGACY.UNKNOWN'",
            "INTRINSIC_QUALITY_STATUS = 'REVIEW_REQUIRED'",
            "SELECTION_ELIGIBILITY = 'INELIGIBLE'",
        ):
            self.assertIn(required, self.compact)

    def test_migration_is_transaction_neutral_and_does_not_rewrite_data(self):
        for forbidden in (
            "BEGIN;", "COMMIT;", "UPDATE ", "DELETE ", "TRUNCATE ",
            "DROP TABLE", "DROP COLUMN",
        ):
            self.assertNotIn(forbidden, self.compact)

    def test_constraints_are_replaced_deterministically_for_safe_reexecution(self):
        self.assertIn("DROP CONSTRAINT IF EXISTS FUNDAMENTALS_RAW_METRIC_CHECK", self.compact)
        self.assertIn("DROP CONSTRAINT IF EXISTS FUNDAMENTALS_NORMALIZED_METRIC_CHECK", self.compact)
        self.assertIn("DROP CONSTRAINT IF EXISTS FUNDAMENTALS_NORMALIZED_UNIT_CHECK", self.compact)


if __name__ == "__main__":
    unittest.main()
