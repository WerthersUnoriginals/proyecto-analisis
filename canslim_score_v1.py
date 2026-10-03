"""CAN SLIM composite v1 (spec docs/superpowers/specs/2026-10-04-canslim-composite-v1-design.md).

Letter weights and the watch threshold approved by the human on 2026-10-04.
Combines the six company letters into a classic checklist and a weighted
composite; M is the market buy filter, not a weight. The verdict is
informative and educational, not investment advice.
"""

from __future__ import annotations

from typing import Mapping

COMPOSITE_VERSION = "canslim-composite-v1"
LETTER_WEIGHTS = {"C": 20, "A": 15, "N": 15, "S": 10, "L": 25, "I": 15}
WATCH_THRESHOLD = 70.0
INTEGRITY_FIELDS = {"C": "data_integrity", "A": "annual_data_integrity", "N": "price_data_integrity",
                    "S": "s_data_integrity", "L": "l_data_integrity", "I": "i_data_integrity"}
CLASSIC_PASS = {"C": {"PASS", "PASS_WITH_DECELERATION"}}
MARKET_SIGNALS = {"CONFIRMED_UPTREND": "BUY_ALLOWED", "UPTREND_UNDER_PRESSURE": "CAUTION", "CORRECTION": "NO_BUY"}


def _letter(code: str, result: Mapping | None) -> dict:
    if not result:
        return {"score": None, "classic": None, "integrity": None}
    key = code.lower()
    score = result.get("score") or {}
    return {
        "score": (score.get(f"{key}_score_v1") or {}).get("normalized_score"),
        "classic": (score.get(f"{key}_classic") or {}).get("result"),
        "integrity": (result.get("contract") or {}).get(INTEGRITY_FIELDS[code]),
    }


def build_canslim_score(letters: Mapping[str, Mapping | None], market: Mapping | None) -> dict:
    detail = {code: _letter(code, letters.get(code)) for code in LETTER_WEIGHTS}
    passed = [code for code, item in detail.items() if item["classic"] in CLASSIC_PASS.get(code, {"PASS"})]
    failed = [code for code in LETTER_WEIGHTS if code not in passed]
    missing = [code for code, item in detail.items() if item["score"] is None]
    review = [code for code, item in detail.items() if item["integrity"] == "REVIEW_REQUIRED"]
    partial = [code for code, item in detail.items() if "PARTIAL" in str(item["integrity"] or "")]

    available = {code: item["score"] for code, item in detail.items() if item["score"] is not None}
    weight = sum(LETTER_WEIGHTS[code] for code in available)
    composite = round(sum(LETTER_WEIGHTS[code] * score for code, score in available.items()) / weight, 2) \
        if weight else None

    data_status = "REVIEW" if review or missing else "PARTIAL" if partial else "OK"
    state = ((market or {}).get("contract") or {}).get("market_state")
    signal = MARKET_SIGNALS.get(state, "UNKNOWN")
    all_six = len(passed) == len(LETTER_WEIGHTS)
    if all_six and data_status != "REVIEW":
        verdict = "CANDIDATE" if signal == "BUY_ALLOWED" else "CANDIDATE_MARKET_NOT_CONFIRMED"
    elif composite is not None and composite >= WATCH_THRESHOLD:
        verdict = "WATCH"
    else:
        verdict = "NOT_CANDIDATE"
    return {
        "version": COMPOSITE_VERSION,
        "letters_passed": len(passed),
        "failed_letters": failed,
        "all_six_pass": all_six,
        "composite_score": composite,
        "letters_available": len(available),
        "missing_letters": missing,
        "review_letters": review,
        "data_status": data_status,
        "market_state": state,
        "market_signal": signal,
        "verdict": verdict,
        "letters": detail,
    }
