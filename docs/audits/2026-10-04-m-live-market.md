# M v1 Live Run (Market Direction)

**Date:** 2026-10-04 (as_of 2026-10-03T22:08Z, last session 2026-10-02)

**Scope:** first live evaluation of M (`m-input-contract-v1`, M Score v1
`m-1.0-exp`, thresholds of spec §4 with correction rule B and weights of §7,
approved 2026-10-04). Reproduce with `python -m database.market_v1` and
`python -m database.m_v3_runner`.

## Evidence

- SPY and QQQ daily bars, 1,506 sessions each (QQQ new; QQQ registered as a
  benchmark by CIK and name because SEC lists no ticker for its trust).
- Breadth over 500 S&P 500 members with a fresh 50-session history.

## Result

| | SPY (S&P 500) | QQQ (Nasdaq-100) |
|---|---|---|
| State | UPTREND_UNDER_PRESSURE (since 2026-08-28) | CONFIRMED_UPTREND (since 2026-09-22) |
| Active distribution days | 10 | 2 |
| Last follow-through day | 2026-04-08 | 2026-08-04 |
| vs 50-session average | +0.8% | +4.6% |
| vs 200-session average | +6.8% | +12.1% |

**Market: UPTREND_UNDER_PRESSURE**, breadth 24.8% of members above their
50-session average. M Score 46.44 (state 25, distribution 0, moving averages
20, breadth 1.4); classic `FAIL_UPTREND_UNDER_PRESSURE`.

## Checks

- Distribution days recomputed directly from Yahoo over the last 25 sessions:
  SPY 10 (same dates), QQQ 5 before the 5% expiry rule (3 expired, 2 active).
- With the index volume instead of ETF volume: ^GSPC 6, ^IXIC 4.

## Findings

| # | Finding | Status |
|---|---|---|
| 1 | Distribution days before a follow-through day kept counting: QQQ fell back into correction the day after its 2026-08-04 follow-through. | Fixed: a follow-through day clears the earlier count (test proven to fail without the fix). |
| 2 | ETF volume inflates distribution days (SPY 10 vs ^GSPC 6). Under rule A (6 distribution days end an uptrend) the market read as a correction with the S&P 1% below its high and above both averages. | Human chose rule B: distribution days only put an uptrend under pressure; a correction needs an 8% drawdown. |
