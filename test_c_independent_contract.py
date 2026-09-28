import unittest
from datetime import datetime, timezone

from database.c_fundamentals_adapter import build_independent_c_input_report
from database.c_live_diagnostic import diagnose_c_snapshots
from database.c_live_runner import _persisted_snapshot, run_live_c_diagnostic

from test_c_live_runner import EXPECTED_REPORT, _legacy_snapshot, _rows


class IndependentCContractTests(unittest.TestCase):
    def test_report_contains_all_eleven_inputs_with_one_as_of(self):
        base = {
            "latest_eps_yoy_pct": 1.0,
            "previous_eps_yoy_pct": 0.5,
            "eps_acceleration_pp": 0.5,
            "latest_revenue_yoy_pct": 2.0,
            "previous_revenue_yoy_pct": 1.0,
            "revenue_acceleration_pp": 1.0,
            "latest_eps": 3.0,
            "eps_yoy_pct": [{"date": "2025-01-01", "value": 1.0}],
            "eps_loss_to_profit": False,
        }
        integrity = {"data_integrity": "VERIFIED", "split_integrity_status": "NO_RECENT_SPLITS"}
        as_of = datetime(2026, 9, 24, tzinfo=timezone.utc)
        report = build_independent_c_input_report(base, integrity, company_id=1, as_of=as_of)
        self.assertEqual(set(report["c_input_contract"]["inputs"]), {
            "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
            "latest_revenue_yoy_pct", "previous_revenue_yoy_pct",
            "revenue_acceleration_pp", "latest_eps", "eps_yoy_pct",
            "eps_loss_to_profit", "data_integrity", "split_integrity_status",
        })
        self.assertTrue(report["independently_reconstructed_by_new_architecture"])
        self.assertEqual({item["as_of"] for item in report["input_provenance"].values()}, {as_of.isoformat()})
        self.assertEqual(report["data_integrity"], "VERIFIED")

    def test_diagnostic_uses_persisted_integrity_without_legacy_complements(self):
        as_of = datetime(2026, 9, 24, tzinfo=timezone.utc)
        persisted = _persisted_snapshot({
            "fundamental_rows": _rows(),
            "fundamental_report": EXPECTED_REPORT,
            "integrity": {
                "data_integrity": "VERIFIED",
                "split_integrity_status": "NO_RECENT_SPLITS",
            },
            "company_id": 1,
            "as_of": as_of,
        })
        result = diagnose_c_snapshots(
            _legacy_snapshot(), persisted,
            capture_metadata={"persisted_read_at": as_of.isoformat()},
        )
        self.assertTrue(result["score_isolation"]["independent_inputs"])
        self.assertIsNone(result["score_isolation"]["shared_complements"])
        self.assertEqual(result["semantic_gaps"], [])

    def test_runner_does_not_forward_legacy_complements_for_independent_capture(self):
        as_of = datetime(2026, 9, 24, tzinfo=timezone.utc)
        payload = {
            "fundamental_rows": _rows(),
            "fundamental_report": EXPECTED_REPORT,
            "integrity": {"data_integrity": "VERIFIED", "split_integrity_status": "NO_RECENT_SPLITS"},
            "company_id": 1, "as_of": as_of,
        }
        result = run_live_c_diagnostic(
            "TST", 1,
            load_persisted_rows=lambda _: payload,
            acquire_legacy_snapshot=lambda *args, **kwargs: _legacy_snapshot(),
            clock=lambda: as_of,
        )
        self.assertTrue(result["score_isolation"]["independent_inputs"])
        self.assertIsNone(result["score_isolation"]["shared_complements"])
        self.assertEqual(result["metadata"]["diagnostic_started_at"], as_of.isoformat())


if __name__ == "__main__":
    unittest.main()
