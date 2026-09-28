import re
import unittest
from pathlib import Path


MIGRATION = (
    Path(__file__).parent
    / "database"
    / "migrations"
    / "2026-09-23_corporate_action_capture_v1.sql"
)


class CorporateActionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.compact = re.sub(r"\s+", " ", cls.sql).strip().upper()

    def test_creates_parent_and_child_append_only_tables(self):
        self.assertRegex(self.compact, r"^-- .* BEGIN;")
        self.assertTrue(self.compact.endswith("COMMIT;"))
        self.assertIn("CREATE TABLE PUBLIC.CORPORATE_ACTION_CAPTURES", self.compact)
        self.assertIn("CREATE TABLE PUBLIC.CORPORATE_ACTION_EVENTS", self.compact)
        self.assertIn("CAPTURE_UID UUID NOT NULL UNIQUE", self.compact)
        self.assertIn("UNIQUE (CAPTURE_ID, EVENT_ORDINAL)", self.compact)

    def test_contract_has_required_foreign_keys_and_restricts_deletes(self):
        self.assertRegex(
            self.compact,
            r"COMPANY_ID BIGINT NOT NULL REFERENCES PUBLIC\.COMPANIES\s*\(ID\) ON DELETE RESTRICT",
        )
        self.assertRegex(
            self.compact,
            r"CAPTURE_ID BIGINT NOT NULL REFERENCES PUBLIC\.CORPORATE_ACTION_CAPTURES\s*\(ID\) ON DELETE RESTRICT",
        )

    def test_distinguishes_status_completeness_and_versioned_fingerprint(self):
        for required in (
            "ACQUISITION_STATUS",
            "COMPLETENESS_STATUS",
            "RESPONSE_FINGERPRINT_VERSION",
            "RESPONSE_FINGERPRINT",
            "PROVIDER_METADATA JSONB",
            "ERROR_METADATA JSONB",
        ):
            self.assertIn(required, self.compact)
        self.assertIn("SPLIT_RATIO <> 'NAN'::NUMERIC", self.compact)
        self.assertGreaterEqual(self.compact.count("RESPONSE_FINGERPRINT_VERSION IS NOT NULL"), 2)
        self.assertGreaterEqual(self.compact.count("RESPONSE_FINGERPRINT IS NOT NULL"), 2)
        self.assertIn("ERROR_CODE ~ '^[A-Z][A-Z0-9_]{0,63}$'", self.compact)

    def test_legacy_contract_requires_exact_six_year_open_window(self):
        self.assertIn("REQUESTED_WINDOW_END_AT IS NULL", self.compact)
        self.assertIn("REQUESTED_WINDOW_START_AT = CAPTURE_STARTED_AT - INTERVAL '6 YEARS'", self.compact)

    def test_split_ratio_storage_is_not_scale_limited(self):
        self.assertIn("SPLIT_RATIO NUMERIC NOT NULL", self.compact)
        self.assertNotIn("SPLIT_RATIO NUMERIC(30, 12)", self.compact)
        self.assertIn("SPLIT_RATIO < 'INFINITY'::NUMERIC", self.compact)
        self.assertIn("SPLIT_RATIO <> 'NAN'::NUMERIC", self.compact)

    def test_migration_is_deliberately_one_shot(self):
        self.assertNotIn("CREATE TABLE IF NOT EXISTS", self.compact)
        self.assertNotIn("CREATE INDEX IF NOT EXISTS", self.compact)

    def test_all_project_objects_are_schema_qualified(self):
        self.assertIn("CREATE TABLE PUBLIC.CORPORATE_ACTION_CAPTURES", self.compact)
        self.assertIn("CREATE TABLE PUBLIC.CORPORATE_ACTION_EVENTS", self.compact)
        self.assertIn("ON PUBLIC.CORPORATE_ACTION_CAPTURES", self.compact)

    def test_has_point_in_time_revision_and_event_indexes(self):
        for fragment in (
            "COMPANY_ID, SOURCE_VARIANT, CAPTURE_CONTRACT_VERSION, OBSERVED_AT DESC, ID DESC",
            "PROVIDER, PROVIDER_CAPTURE_ID, PROVIDER_REVISION_ID",
            "CAPTURE_ID, EVENT_DATE, EVENT_ORDINAL",
        ):
            self.assertIn(fragment, self.compact)

    def test_create_index_names_are_not_schema_qualified(self):
        for index_name in (
            "CORPORATE_ACTION_CAPTURES_POINT_IN_TIME_IDX",
            "CORPORATE_ACTION_CAPTURES_PROVIDER_REVISION_IDX",
            "CORPORATE_ACTION_EVENTS_CAPTURE_DATE_IDX",
        ):
            self.assertIn(f"CREATE INDEX {index_name} ON PUBLIC.", self.compact)
            self.assertNotIn(f"CREATE INDEX PUBLIC.{index_name}", self.compact)

    def test_does_not_change_roles_or_existing_tables(self):
        for forbidden in (
            r"\bGRANT\b",
            r"\bREVOKE\b",
            r"\bALTER ROLE\b",
            r"\bALTER TABLE FUNDAMENTALS_",
            r"\bDROP\b",
            r"\bUPDATE\s+\w+\s+SET\b",
            r"\bDELETE\s+FROM\b",
            r"\bTRUNCATE\b",
        ):
            self.assertNotRegex(self.compact, forbidden)
        self.assertNotIn("SPLIT_INTEGRITY_STATUS", self.compact)
        self.assertNotIn("DATA_INTEGRITY", self.compact)


if __name__ == "__main__":
    unittest.main()
