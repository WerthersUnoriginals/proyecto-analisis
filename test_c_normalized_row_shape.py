import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.c_fundamentals_adapter import normalized_rows_to_effective_rows
from database.c_live_runner import _build_persisted_lineage
from database.c_live_runner import _default_load_persisted_rows
from unittest.mock import patch


def normalized_row(row_id=7):
    return {
        "id": row_id, "company_id": 1, "metric": "EPS_DILUTED", "source": "SEC",
        "source_variant": "sec.company_facts", "observation_kind": "REPORTED",
        "value": Decimal("2"), "unit": "USD/shares", "currency": "USD",
        "source_period_start": date(2025, 1, 1), "source_period_end": date(2025, 3, 31),
        "canonical_period_end": date(2025, 3, 31), "series_date": date(2025, 3, 31),
        "fiscal_year": 2025, "fiscal_quarter": 1, "filed_date": date(2025, 5, 1),
        "source_available_at": datetime(2025, 5, 1, tzinfo=timezone.utc),
        "observed_at": datetime(2025, 5, 2, tzinfo=timezone.utc), "raw_id": 99,
        "normalizer_version": "sec-normalized-v2", "intrinsic_quality_status": "OK",
        "intrinsic_quality_reasons": [], "selection_eligibility": "ELIGIBLE",
        "alignment_method": "EXACT", "alignment_days": 0,
        "alignment_reference_id": None, "source_metric_name": "EarningsPerShareDiluted",
        "source_unit": "USD/shares", "source_scale_factor": Decimal("1"),
        "source_record_id": "0000320193-25-000057",
        "source_identity_type": "SEC_ACCESSION",
    }


class NormalizedRowShapeTests(unittest.TestCase):
    def test_real_normalized_loader_shape_projects_stable_lineage_identity(self):
        rows = normalized_rows_to_effective_rows(
            [normalized_row()], as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selected_observation_id"], 7)
        self.assertEqual(rows[0]["raw_id"], 99)
        self.assertEqual(rows[0]["source_metric_name"], "EarningsPerShareDiluted")
        self.assertEqual(rows[0]["source_record_id"], "0000320193-25-000057")
        self.assertEqual(rows[0]["source_identity_type"], "SEC_ACCESSION")
        self.assertIn("comparison_status", rows[0])
        lineage = _build_persisted_lineage(rows)
        self.assertIn("latest_eps", lineage)
        self.assertEqual(lineage["latest_eps"][0]["selected_observation_id"], 7)

    def test_default_persisted_loader_uses_projected_observations_and_same_as_of(self):
        as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
        integrity = {
            "data_integrity": "REVIEW_REQUIRED",
            "split_integrity_status": "UNKNOWN",
        }
        report = {
            "latest_eps_yoy_pct": 28.0, "previous_eps_yoy_pct": 20.0,
            "eps_acceleration_pp": 8.0, "latest_revenue_yoy_pct": 16.0,
            "previous_revenue_yoy_pct": 15.0, "revenue_acceleration_pp": 1.0,
            "latest_eps": 2.0, "eps_yoy_pct": [{"date": "2025-03-31", "value": 20.0}],
            "eps_loss_to_profit": False,
        }
        seen_scalars = {}

        def reconstruct(*args, **kwargs):
            seen_scalars.update(kwargs["scalars"])
            return integrity

        with patch(
            "database.c_data_integrity.load_normalized_evidence",
            return_value=(normalized_row(),),
        ), patch(
            "database.corporate_actions.load_latest_capture_as_of",
            return_value=(None, ()),
        ), patch(
            "database.c_data_integrity.reconstruct_persisted_integrity",
            side_effect=reconstruct,
        ), patch(
            "database.c_fundamentals_adapter.build_c_fundamental_report_from_normalized",
            return_value=report,
        ):
            payload = _default_load_persisted_rows(1, as_of=as_of)
        self.assertEqual(payload["company_id"], 1)
        self.assertEqual(payload["as_of"], as_of)
        self.assertEqual(payload["lineage_rows"][0]["selected_observation_id"], 7)
        self.assertEqual(
            {key: seen_scalars[key] for key in report if key != "eps_yoy_pct" and key != "latest_eps" and key != "eps_loss_to_profit"},
            {
                "latest_eps_yoy_pct": 28.0, "previous_eps_yoy_pct": 20.0,
                "eps_acceleration_pp": 8.0, "latest_revenue_yoy_pct": 16.0,
                "previous_revenue_yoy_pct": 15.0, "revenue_acceleration_pp": 1.0,
            },
        )

    def test_as_of_keeps_identity_from_visible_normalized_observation(self):
        older = normalized_row(7)
        newer = normalized_row(8)
        newer["raw_id"] = 100
        newer["source_record_id"] = "0000320193-25-000099"
        newer["observed_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = normalized_rows_to_effective_rows(
            [older, newer],
            as_of=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selected_observation_id"], 7)
        self.assertEqual(rows[0]["source_record_id"], "0000320193-25-000057")


if __name__ == "__main__":
    unittest.main()
