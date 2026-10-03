"""Offline tests for batch evaluation of many companies."""

import unittest
from collections import Counter
from datetime import datetime, timezone

from database.batch_v3 import run_batch, summarize

UTC = timezone.utc
AS_OF = datetime(2026, 10, 2, tzinfo=UTC)


def fake_result(score, integrity="VERIFIED", classic="PASS"):
    contract = {"latest_eps": 1.0, "latest_eps_yoy_pct": 30.0, "data_integrity": integrity,
                "split_integrity_status": "NO_RECENT_SPLITS",
                "integrity": {"diagnostics": []}}
    score_block = {"c_score_v1": {"normalized_score": score, "class": "STRONG", "status": "OK",
                                  "usability": "C_SCORE_USABLE"}, "c_flags": []}
    a_contract = {"latest_fiscal_year": 2025, "annual_data_integrity": integrity, "integrity": {"diagnostics": []}}
    a_score = {"a_score_v1": {"normalized_score": score, "class": "STRONG", "status": "OK",
                              "usability": "A_SCORE_USABLE"}, "a_classic": {"result": classic}, "a_flags": []}
    return {"contract": contract, "score": score_block}, {"contract": a_contract, "score": a_score}


class BatchTests(unittest.TestCase):
    def test_failures_do_not_stop_the_batch(self):
        def evaluate(ticker, as_of):
            if ticker == "BAD":
                raise RuntimeError("boom")
            return fake_result(80.0)

        rows = run_batch(["AAA", "BAD", "CCC"], as_of=AS_OF, evaluate=evaluate)
        self.assertEqual([row["ticker"] for row in rows], ["AAA", "BAD", "CCC"])
        self.assertEqual(rows[1]["error"], "RuntimeError")
        self.assertEqual(rows[2]["c_score"], 80.0)

    def test_ingest_runs_first_when_requested(self):
        calls = []
        rows = run_batch(["AAA"], as_of=AS_OF, ingest=lambda ticker: calls.append(("ingest", ticker)),
                         evaluate=lambda ticker, as_of: calls.append(("evaluate", ticker)) or fake_result(1.0))
        self.assertEqual(calls, [("ingest", "AAA"), ("evaluate", "AAA")])
        self.assertIsNone(rows[0]["error"])

    def test_default_as_of_is_taken_after_all_ingestion(self):
        events = []
        clock = lambda: events.append("clock") or datetime(2026, 10, 3, 20, tzinfo=UTC)
        seen = []
        run_batch(["AAA", "BBB"], as_of=None, clock=clock, ingest=lambda ticker: events.append(f"ingest:{ticker}"),
                  evaluate=lambda ticker, as_of: seen.append(as_of) or fake_result(1.0))
        self.assertEqual(events, ["ingest:AAA", "ingest:BBB", "clock"])
        self.assertEqual(seen, [datetime(2026, 10, 3, 20, tzinfo=UTC)] * 2)

    def test_failed_ingestion_is_reported_and_not_evaluated(self):
        def ingest(ticker):
            if ticker == "BAD":
                raise RuntimeError("down")

        evaluated = []
        rows = run_batch(["BAD", "OK"], as_of=AS_OF, ingest=ingest,
                         evaluate=lambda ticker, as_of: evaluated.append(ticker) or fake_result(1.0))
        self.assertEqual(rows[0]["error"], "RuntimeError")
        self.assertEqual(evaluated, ["OK"])

    def test_summary_counts_statuses(self):
        rows = run_batch(["A", "B"], as_of=AS_OF,
                         evaluate=lambda ticker, as_of: fake_result(70.0, "REVIEW_REQUIRED" if ticker == "B" else "VERIFIED"))
        summary = summarize(rows)
        self.assertEqual(summary["companies"], 2)
        self.assertEqual(summary["c_data_integrity"], Counter({"VERIFIED": 1, "REVIEW_REQUIRED": 1}))
        self.assertEqual(summary["errors"], 0)

    def test_n_fields_are_added_when_evaluated(self):
        n_contract = {"n_status": "OK", "price_data_integrity": "VERIFIED", "pct_below_high_52w": 4.2,
                      "new_high_recent": True, "catalysts": {"counts": {"5.02": 1}},
                      "integrity": {"diagnostics": ["HIGH_5Y_INSUFFICIENT_HISTORY"]}}
        n_score = {"n_score_v1": {"normalized_score": 91.0, "usability": "N_SCORE_USABLE"},
                   "n_classic": {"result": "PASS"}}
        rows = run_batch(["A"], as_of=AS_OF,
                         evaluate=lambda ticker, as_of: (*fake_result(70.0), {"contract": n_contract, "score": n_score}))
        self.assertEqual((rows[0]["n_pct_below_high_52w"], rows[0]["n_data_integrity"]), (4.2, "VERIFIED"))
        self.assertEqual((rows[0]["n_score"], rows[0]["n_classic"]), (91.0, "PASS"))
        summary = summarize(rows)
        self.assertEqual(summary["n_data_integrity"], Counter({"VERIFIED": 1}))
        self.assertEqual(summary["diagnostics"]["HIGH_5Y_INSUFFICIENT_HISTORY"], 1)

    def test_s_fields_are_added_when_evaluated(self):
        s_contract = {"s_data_integrity": "VERIFIED", "shares_change_3y_pct": -4.0, "debt_to_equity_latest": 0.5,
                      "up_down_volume_ratio_50d": 1.3, "integrity": {"diagnostics": ["NO_DEBT_EVIDENCE"]}}
        s_score = {"s_score_v1": {"normalized_score": 77.0, "usability": "S_SCORE_USABLE"},
                   "s_classic": {"result": "PASS"}}
        n_result = {"contract": {"n_status": "OK", "price_data_integrity": "VERIFIED", "pct_below_high_52w": 1.0,
                                 "new_high_recent": False, "catalysts": {"counts": {}},
                                 "integrity": {"diagnostics": []}}}
        rows = run_batch(["A"], as_of=AS_OF, evaluate=lambda ticker, as_of: (
            *fake_result(70.0), n_result, {"contract": s_contract, "score": s_score}))
        self.assertEqual((rows[0]["s_score"], rows[0]["s_classic"], rows[0]["s_volume_ratio"]), (77.0, "PASS", 1.3))
        summary = summarize(rows)
        self.assertEqual(summary["s_classic"], Counter({"PASS": 1}))
        self.assertEqual(summary["diagnostics"]["NO_DEBT_EVIDENCE"], 1)

    def test_l_fields_are_added_when_evaluated(self):
        l_contract = {"l_data_integrity": "VERIFIED", "rs_rating": 93, "group_key": "36", "group_rank_pct": 88.0,
                      "integrity": {"diagnostics": ["RS_LINE_HIGH_BEFORE_PRICE"]}}
        l_score = {"l_score_v1": {"normalized_score": 90.0, "usability": "L_SCORE_USABLE"},
                   "l_classic": {"result": "PASS"}}
        rows = run_batch(["A"], as_of=AS_OF, evaluate=lambda ticker, as_of: (
            *fake_result(70.0), None, None, {"contract": l_contract, "score": l_score}))
        self.assertEqual((rows[0]["l_rs_rating"], rows[0]["l_classic"]), (93, "PASS"))
        self.assertNotIn("n_score", rows[0])
        self.assertEqual(summarize(rows)["l_classic"], Counter({"PASS": 1}))

    def test_i_fields_are_added_when_evaluated(self):
        i_contract = {"i_data_integrity": "VERIFIED", "holders_latest": 900, "holders_change_qoq_pct": 3.0,
                      "institutional_ownership_pct": 70.0, "integrity": {"diagnostics": ["CUSIP_FROM_NAME_MATCH"]}}
        i_score = {"i_score_v1": {"normalized_score": 81.0, "usability": "I_SCORE_USABLE"},
                   "i_classic": {"result": "PASS"}}
        rows = run_batch(["A"], as_of=AS_OF, evaluate=lambda ticker, as_of: (
            *fake_result(70.0), None, None, None, {"contract": i_contract, "score": i_score}))
        self.assertEqual((rows[0]["i_holders"], rows[0]["i_classic"]), (900, "PASS"))
        summary = summarize(rows)
        self.assertEqual(summary["i_classic"], Counter({"PASS": 1}))
        self.assertEqual(summary["diagnostics"]["CUSIP_FROM_NAME_MATCH"], 1)

    def test_market_fields_are_shared(self):
        m_result = {"contract": {"market_state": "CONFIRMED_UPTREND", "m_data_integrity": "VERIFIED"},
                    "score": {"m_score_v1": {"normalized_score": 92.0}, "m_classic": {"result": "PASS"}}}
        rows = run_batch(["A", "B"], as_of=AS_OF, evaluate=lambda ticker, as_of: (
            *fake_result(70.0), None, None, None, None, m_result))
        self.assertEqual({row["m_market_state"] for row in rows}, {"CONFIRMED_UPTREND"})
        self.assertEqual(summarize(rows)["market"]["m_score"], 92.0)

    def test_composite_and_ranking(self):
        from database.batch_v3 import _canslim_fields
        from test_canslim_score_v1 import letter, market

        fields = _canslim_fields({code: letter(code, 90.0) for code in "CANSLI"}, market())
        self.assertEqual((fields["canslim_verdict"], fields["canslim_composite"]), ("CANDIDATE", 90.0))
        self.assertEqual(_canslim_fields({**{code: letter(code) for code in "CANSL"}, "I": None}, market()), {})
        rows = [{"ticker": ticker, "error": None, "c_data_integrity": "VERIFIED", "c_usability": "X",
                 "a_data_integrity": "VERIFIED", "a_classic": "PASS", "c_diagnostics": [], "a_diagnostics": [],
                 "canslim_composite": composite, "canslim_letters_passed": 6, "canslim_failed_letters": [],
                 "canslim_data_status": "OK", "canslim_verdict": "CANDIDATE"}
                for ticker, composite in (("LOW", 50.0), ("TOP", 90.0))]
        summary = summarize(rows)
        self.assertEqual([item["ticker"] for item in summary["ranking"]], ["TOP", "LOW"])
        self.assertEqual(summary["canslim_verdict"], Counter({"CANDIDATE": 2}))

if __name__ == "__main__":
    unittest.main()
