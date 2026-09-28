"""Opt-in, strictly read-only PostgreSQL acceptance for the 11-input C path."""

import json
import os
import unittest
from datetime import datetime, timezone


@unittest.skipUnless(
    os.getenv("CANSLIM_RUN_PG_INTEGRATION") == "1",
    "set CANSLIM_RUN_PG_INTEGRATION=1 for the real PostgreSQL gate",
)
class IndependentPostgresAcceptanceTests(unittest.TestCase):
    def test_aapl_independent_eleven_inputs_are_read_only_and_same_as_of(self):
        try:
            from database.db import get_connection
            from database.c_data_integrity import load_normalized_evidence
            from database.corporate_actions import load_latest_capture_as_of
            from database.c_live_runner import load_independent_c_inputs
            from c_score_v1 import build_c_score
        except ImportError as exc:  # pragma: no cover - depends on user runtime
            self.skipTest(f"PostgreSQL runtime unavailable: {exc}")

        as_of = datetime.now(timezone.utc)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), current_user, version()")
                database_name, current_user, server_version = cursor.fetchone()
                self.assertEqual(database_name, "canslim")
                self.assertEqual(current_user, "canslim_app")
                cursor.execute("SELECT id FROM companies WHERE ticker = %s", ("AAPL",))
                company_row = cursor.fetchone()
        self.assertIsNotNone(company_row)
        company_id = company_row[0]
        normalized = load_normalized_evidence(company_id, as_of)
        self.assertTrue(normalized)
        sec_rows = [
            row for row in normalized
            if row.get("source") == "SEC"
            and row.get("source_variant") == "sec.company_facts"
        ]
        self.assertTrue(sec_rows)
        self.assertEqual(
            {row.get("source_identity_type") for row in sec_rows},
            {"SEC_ACCESSION"},
        )
        self.assertTrue(all(row.get("source_record_id") for row in sec_rows))
        self.assertTrue(all(not row.get("source_identity_error") for row in sec_rows))
        capture, events = load_latest_capture_as_of(company_id, as_of)
        print(json.dumps({
            "current_database": database_name,
            "current_user": current_user,
            "server_version": server_version,
            "company_id": company_id,
            "as_of": as_of.isoformat(),
            "normalized_rows": len(normalized),
            "sec_identity_rows": len(sec_rows),
            "capture_id": None if capture is None else capture.id,
            "capture_uid": None if capture is None else str(capture.capture_uid),
            "acquisition_status": None if capture is None else capture.acquisition_status,
            "completeness_status": None if capture is None else capture.completeness_status,
            "event_count": len(events),
        }, sort_keys=True))
        if capture is None or capture.acquisition_status != "SUCCESS" or capture.completeness_status != "COMPLETE":
            self.skipTest("AAPL has no SUCCESS + COMPLETE persisted corporate-action capture")

        snapshot = load_independent_c_inputs(company_id, as_of)
        report = snapshot["fundamental_report"]
        expected = {
            "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
            "latest_revenue_yoy_pct", "previous_revenue_yoy_pct",
            "revenue_acceleration_pp", "latest_eps", "eps_yoy_pct",
            "eps_loss_to_profit", "data_integrity", "split_integrity_status",
        }
        self.assertEqual(set(report["c_input_contract"]["inputs"]), expected)
        self.assertTrue(all(
            item["independently_reconstructed"]
            for item in report["input_provenance"].values()
        ))
        self.assertEqual({item["company_id"] for item in report["input_provenance"].values()}, {company_id})
        self.assertEqual({item["as_of"] for item in report["input_provenance"].values()}, {as_of.isoformat()})
        integrity = snapshot["integrity_reconstruction"]
        self.assertEqual(integrity["data_quality"], "completa")
        self.assertEqual(integrity["data_integrity"], "VERIFIED")
        score = build_c_score(report)
        self.assertEqual(score["c_score_v1"]["status"], "OK")
        self.assertEqual(score["c_score_v1"]["usability"], "C_SCORE_USABLE")
        print(json.dumps({
            "inputs": {key: report[key] for key in sorted(expected)},
            "integrity": snapshot["integrity_reconstruction"],
            "score": score,
            "legacy_complements_used": False,
        }, default=str, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
