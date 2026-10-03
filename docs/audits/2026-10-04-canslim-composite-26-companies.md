# CAN SLIM Composite v1 on 26 Companies

**Date:** 2026-10-04 (as_of 2026-10-03T22:15Z)

**Scope:** first live run of `canslim-composite-v1` with the letter weights
(C 20, A 15, N 15, S 10, L 25, I 15) and watch threshold 70 approved on
2026-10-04,
over the seven v1 letters. Reproduce with `python -m database.batch_v3 <tickers>`.

**Market (M):** `UPTREND_UNDER_PRESSURE` → signal `CAUTION`.

## Ranking

| # | Ticker | Composite | Letters passed | Failed | Data | Verdict |
|---|---|---|---|---|---|---|
| 1 | NVDA | 87.06 | 6/6 | — | OK | CANDIDATE_MARKET_NOT_CONFIRMED |
| 2 | TSM | 84.08 | 3/6 | C, A, S | REVIEW (foreign) | WATCH |
| 3 | ASML | 73.82 | 3/6 | C, A, S | REVIEW (foreign) | WATCH |
| 4 | LRCX | 71.47 | 4/6 | A, N | OK | WATCH |
| 5 | MSFT | 69.03 | 5/6 | A | OK | NOT_CANDIDATE |
| 6 | PLTR | 67.94 | 3/6 | A, S, I | PARTIAL | NOT_CANDIDATE |
| 7 | AAPL | 66.34 | 5/6 | A | OK | NOT_CANDIDATE |
| 8 | SMCI | 63.52 | 3/6 | A, N, S | OK | NOT_CANDIDATE |
| 9 | JPM | 62.61 | 4/6 | A, L | REVIEW | NOT_CANDIDATE |
| 10 | XOM | 59.43 | 5/6 | A | REVIEW (I) | NOT_CANDIDATE |
| 11 | BRK-B | 53.78 | 3/6 | A, S, L | REVIEW | NOT_CANDIDATE |
| 12 | AVGO | 52.55 | 2/6 | A, N, S, L | OK | NOT_CANDIDATE |
| 13 | BAC | 46.64 | 3/6 | A, N, L | OK | NOT_CANDIDATE |
| 14 | UBER | 44.18 | 2/6 | A, N, S, L | PARTIAL | NOT_CANDIDATE |
| 15 | ORCL | 43.05 | 2/6 | A, N, S, L | PARTIAL | NOT_CANDIDATE |
| 16 | RDDT | 43.02 | 2/6 | A, N, S, L | PARTIAL | NOT_CANDIDATE |
| 17 | PLD | 42.10 | 2/6 | A, N, S, L | OK | NOT_CANDIDATE |
| 18 | COST | 41.78 | 2/6 | C, A, N, L | OK | NOT_CANDIDATE |
| 19 | NEE | 36.07 | 2/6 | A, N, S, L | OK | NOT_CANDIDATE |
| 20 | O | 33.74 | 2/6 | A, N, S, L | REVIEW | NOT_CANDIDATE |
| 21 | CMG | 33.72 | 1/6 | C, A, N, L, I | OK | NOT_CANDIDATE |
| 22 | WMT | 33.41 | 0/6 | all | OK | NOT_CANDIDATE |
| 23 | CRWV | 26.76 | 1/6 | C, A, N, S, L | REVIEW | NOT_CANDIDATE |
| 24 | RIVN | 21.99 | 1/6 | C, A, N, S, L | PARTIAL | NOT_CANDIDATE |
| 25 | LCID | 21.85 | 1/6 | C, A, N, S, L | REVIEW | NOT_CANDIDATE |
| 26 | SNAP | 19.55 | 0/6 | all | PARTIAL | NOT_CANDIDATE |

Verdicts: CANDIDATE_MARKET_NOT_CONFIRMED 1, WATCH 3, NOT_CANDIDATE 22.

## Observations

- NVDA is the only company passing all six letters; with the market under
  pressure it is a candidate waiting for a confirmed market, as O'Neil would
  have it.
- A is the strictest letter: its classic rule (EPS growth of at least 25% in
  each of the last three years, ROE of at least 17%, no loss year) fails for
  25 of 26 companies. MSFT, AAPL and XOM pass every other letter.
- TSM and ASML score high but stay in review (foreign filers), so they can
  only be watched, never be candidates.
