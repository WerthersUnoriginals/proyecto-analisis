"""Offline tests for the N input contract (n-input-contract-v1)."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from database.catalysts_v3 import select_catalysts
from database.n_contract_v1 import CONTRACT_VERSION, N_INPUT_KEYS, build_n_contract_v1
from database.new_highs_v1 import compute_highs
from database.prices_v3 import PriceSeries
from database.split_basis import SplitReconciliation
from test_new_highs_v1 import series

UTC = timezone.utc
AS_OF = datetime(2026, 10, 2, 22, tzinfo=UTC)
NO_SPLITS = SplitReconciliation("NO_RECENT_SPLITS", ())


def contract(bars, *, series_review=(), split_status="NO_RECENT_SPLITS", filer_status="DOMESTIC", catalysts=None):
    price_series = PriceSeries(bars=tuple(bars), review_reasons=tuple(series_review))
    highs = compute_highs(price_series.bars, AS_OF.date())
    return build_n_contract_v1(
        price_series, highs, NO_SPLITS, split_status=split_status, split_reasons=[], rejected_splits=(),
        company_id=1, as_of=AS_OF, filer_status=filer_status,
        catalysts=catalysts or select_catalysts([], AS_OF),
    )


def long_series(last_close=95):
    return series([100] * 1400 + [last_close])


class ShapeTests(unittest.TestCase):
    def test_all_inputs_and_version(self):
        result = contract(long_series())
        self.assertEqual(CONTRACT_VERSION, "n-input-contract-v1")
        for key in N_INPUT_KEYS:
            self.assertIn(key, result)
            self.assertIn(key, result["n_input_contract"]["inputs"])
        self.assertEqual(result["pct_below_high_52w"], 5.0)
        self.assertEqual(result["price_data_integrity"], "VERIFIED")
        self.assertEqual(result["n_status"], "OK")
        versions = result["n_input_contract"]
        self.assertEqual(
            (versions["price_series_version"], versions["highs_version"], versions["split_basis_version"]),
            ("price-series-v1", "n-highs-v1", "split-basis-v1"),
        )

    def test_catalysts_are_attached_but_never_change_integrity(self):
        from test_catalysts_v3 import filing

        catalysts = select_catalysts([filing("0001045810-26-000001", date(2026, 9, 1), ["5.02"])], AS_OF)
        with_events = contract(long_series(), catalysts=catalysts)
        without = contract(long_series())
        self.assertEqual(len(with_events["catalysts"]["events"]), 1)
        self.assertEqual(
            {key: with_events[key] for key in N_INPUT_KEYS}, {key: without[key] for key in N_INPUT_KEYS},
        )


class IntegrityTests(unittest.TestCase):
    def test_no_prices_requires_review(self):
        result = contract([])
        self.assertEqual(result["price_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("NO_PRICE_EVIDENCE", result["integrity"]["diagnostics"])

    def test_stale_prices_require_review(self):
        result = contract(series([100] * 300, end=date(2026, 9, 18)))
        self.assertEqual(result["n_status"], "STALE_PRICES")
        self.assertEqual(result["price_data_integrity"], "REVIEW_REQUIRED")

    def test_recent_ipo_is_partial_not_review(self):
        result = contract(series([100] * 100))
        self.assertEqual(result["n_status"], "INSUFFICIENT_HISTORY")
        self.assertEqual(result["price_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")
        self.assertIsNone(result["pct_below_high_52w"])
        self.assertEqual(result["n_input_contract"]["inputs"]["high_52w"]["reasons"], ["INSUFFICIENT_PRICE_HISTORY"])

    def test_missing_five_year_high_is_partial(self):
        result = contract(series([100] * 300))
        self.assertEqual(result["price_data_integrity"], "VERIFIED_WITH_PARTIAL_CORE_DATA")

    def test_series_review_reason_requires_review(self):
        result = contract(long_series(), series_review=["PRICE_BASIS_CONFLICT"])
        self.assertEqual(result["price_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("PRICE_BASIS_CONFLICT", result["integrity"]["diagnostics"])

    def test_split_review_propagates(self):
        result = contract(long_series(), split_status="REVIEW_REQUIRED")
        self.assertEqual(result["price_data_integrity"], "REVIEW_REQUIRED")
        self.assertIn("SPLIT_STATUS:REVIEW_REQUIRED", result["integrity"]["diagnostics"])

    def test_foreign_filer_requires_review(self):
        result = contract(long_series(), filer_status="FOREIGN_FILER_NOT_SUPPORTED")
        self.assertEqual(result["price_data_integrity"], "REVIEW_REQUIRED")


class RunnerTests(unittest.TestCase):
    def test_runner_wires_evidence_into_the_contract(self):
        from unittest import mock

        from database import evidence_v3
        from database.n_v3_runner import evaluate_n
        from database.prices_v3 import PriceBar

        raw = [
            PriceBar(bar_date=item.bar_date, open=item.open, high=item.high, low=item.low, close=item.close,
                     adj_close=item.close, volume=1000, currency="USD", observed_at=item.observed_at)
            for item in long_series()
        ]
        with (
            mock.patch.object(evidence_v3, "load_sec_facts", return_value=[]),
            mock.patch.object(evidence_v3, "load_price_bars", return_value=raw),
            mock.patch.object(evidence_v3, "load_filing_forms", return_value=[("10-K", date(2026, 2, 1))]),
            mock.patch.object(evidence_v3, "load_catalyst_filings", return_value=[]),
        ):
            result = evaluate_n(1, datetime(2026, 10, 3, 12, tzinfo=UTC), capture_loader=lambda company, as_of: (None, []))
        contract = result["contract"]
        self.assertEqual(contract["pct_below_high_52w"], 5.0)
        # No visible split capture: the split status is unknown, so N fails closed.
        self.assertEqual(contract["split_integrity_status"], "UNKNOWN")
        self.assertEqual(contract["price_data_integrity"], "REVIEW_REQUIRED")
        self.assertEqual(result["evidence"]["sessions"], len(raw))
        self.assertEqual(result["score"]["n_score_v1"]["status"], "REVIEW_REQUIRED_DATA")


if __name__ == "__main__":
    unittest.main()
