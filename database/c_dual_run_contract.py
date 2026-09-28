"""Versioned, offline-only contract for the C dual-run fixtures.

This module defines comparison policy and fixture loading only. It deliberately
contains no report comparator, live acquisition, or database access.
"""

from __future__ import annotations

import json
from pathlib import Path


CONTRACT_VERSION = "c-v2.6-compatible-v1"
FIXTURE_VERSION = "aapl-c-dual-run-v1"
TOLERANCE_CONTRACT_VERSION = "c-dual-run-tolerances-v1"

FUNDAMENTAL_INPUT_KEYS = (
    "latest_eps_yoy_pct",
    "previous_eps_yoy_pct",
    "eps_acceleration_pp",
    "latest_revenue_yoy_pct",
    "previous_revenue_yoy_pct",
    "revenue_acceleration_pp",
    "latest_eps",
    "eps_yoy_pct",
    "eps_loss_to_profit",
)

INDEPENDENT_C_INPUT_KEYS = FUNDAMENTAL_INPUT_KEYS + (
    "data_integrity",
    "split_integrity_status",
)

COMPARISON_POLICY = {
    "eps": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-7},
    "large_monetary": {"mode": "numeric", "abs_tol": 0.01, "rel_tol": 1e-10},
    "yoy_pct": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
    "acceleration_pp": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
    "component_points": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
    "raw_points": {"mode": "numeric", "abs_tol": 1e-8, "rel_tol": 1e-9},
    "available_points": {"mode": "exact"},
    "normalized_score": {"mode": "exact"},
    "dates": {"mode": "exact"},
    "source": {"mode": "exact"},
    "source_variant": {"mode": "exact"},
    "none_and_key_presence": {"mode": "exact"},
    "class_status_usability": {"mode": "exact"},
    "flags": {"mode": "exact_set"},
    "diagnostic": {"mode": "exact"},
    "classic": {"mode": "exact"},
}

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "aapl_c_dual_run_v1.json"


def load_aapl_dual_run_fixture() -> dict:
    """Load the frozen AAPL dual-run contract without external dependencies."""
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
