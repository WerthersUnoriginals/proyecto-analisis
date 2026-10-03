"""Offline tests for the CAN SLIM composite (canslim-composite-v1)."""

import unittest

from canslim_score_v1 import COMPOSITE_VERSION, LETTER_WEIGHTS, build_canslim_score

INTEGRITY = {"C": "data_integrity", "A": "annual_data_integrity", "N": "price_data_integrity",
             "S": "s_data_integrity", "L": "l_data_integrity", "I": "i_data_integrity"}


def letter(code, score=80.0, classic="PASS", integrity="VERIFIED"):
    key = code.lower()
    return {"contract": {INTEGRITY[code]: integrity},
            "score": {f"{key}_score_v1": {"normalized_score": score}, f"{key}_classic": {"result": classic}}}


def market(state="CONFIRMED_UPTREND"):
    return {"contract": {"market_state": state}}


def letters(**overrides):
    values = {code: letter(code) for code in "CANSLI"}
    values.update(overrides)
    return values


class CompositeTests(unittest.TestCase):
    def test_version_and_weights(self):
        self.assertEqual(COMPOSITE_VERSION, "canslim-composite-v1")
        self.assertEqual(sum(LETTER_WEIGHTS.values()), 100)

    def test_all_pass_in_confirmed_market_is_a_candidate(self):
        result = build_canslim_score(letters(), market())
        self.assertEqual(result["letters_passed"], 6)
        self.assertTrue(result["all_six_pass"])
        self.assertEqual(result["composite_score"], 80.0)
        self.assertEqual((result["market_signal"], result["verdict"]), ("BUY_ALLOWED", "CANDIDATE"))

    def test_weighted_composite(self):
        result = build_canslim_score(letters(L=letter("L", 100.0), S=letter("S", 0.0)), market())
        self.assertEqual(result["composite_score"], round((80 * 65 + 100 * 25 + 0 * 10) / 100, 2))

    def test_c_deceleration_still_passes(self):
        result = build_canslim_score(letters(C=letter("C", classic="PASS_WITH_DECELERATION")), market())
        self.assertEqual(result["letters_passed"], 6)

    def test_market_is_a_filter_not_a_weight(self):
        for state, signal, verdict in (("UPTREND_UNDER_PRESSURE", "CAUTION", "CANDIDATE_MARKET_NOT_CONFIRMED"),
                                       ("CORRECTION", "NO_BUY", "CANDIDATE_MARKET_NOT_CONFIRMED"),
                                       (None, "UNKNOWN", "CANDIDATE_MARKET_NOT_CONFIRMED")):
            with self.subTest(state=state):
                result = build_canslim_score(letters(), market(state))
                self.assertEqual(result["composite_score"], 80.0)
                self.assertEqual((result["market_signal"], result["verdict"]), (signal, verdict))

    def test_a_failed_letter_is_watch_or_not(self):
        watch = build_canslim_score(letters(A=letter("A", 60.0, "FAIL_EPS_GROWTH")), market())
        self.assertEqual((watch["letters_passed"], watch["failed_letters"], watch["verdict"]), (5, ["A"], "WATCH"))
        weak = build_canslim_score({code: letter(code, 40.0, "FAIL") for code in "CANSLI"}, market())
        self.assertEqual(weak["verdict"], "NOT_CANDIDATE")

    def test_review_letter_blocks_the_candidate_but_keeps_the_score(self):
        result = build_canslim_score(letters(I=letter("I", integrity="REVIEW_REQUIRED")), market())
        self.assertEqual(result["data_status"], "REVIEW")
        self.assertEqual(result["review_letters"], ["I"])
        self.assertEqual(result["composite_score"], 80.0)
        self.assertEqual(result["verdict"], "WATCH")

    def test_missing_letter_renormalizes_and_reviews(self):
        values = letters(S=letter("S", score=None))
        result = build_canslim_score(values, market())
        self.assertEqual(result["letters_available"], 5)
        self.assertEqual(result["missing_letters"], ["S"])
        self.assertEqual(result["data_status"], "REVIEW")
        absent = letters()
        del absent["N"]
        self.assertEqual(build_canslim_score(absent, market())["missing_letters"], ["N"])

    def test_partial_status(self):
        result = build_canslim_score(letters(L=letter("L", integrity="VERIFIED_WITH_PARTIAL_CORE_DATA")), market())
        self.assertEqual(result["data_status"], "PARTIAL")
        self.assertEqual(result["verdict"], "CANDIDATE")

    def test_accounting_difference_is_not_review(self):
        result = build_canslim_score(letters(C=letter("C", integrity="VERIFIED_WITH_ACCOUNTING_DIFFERENCE")), market())
        self.assertEqual(result["data_status"], "OK")


if __name__ == "__main__":
    unittest.main()
