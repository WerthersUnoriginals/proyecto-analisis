# L v1 Live Run on 26 Companies

**Date:** 2026-10-03

**Scope:** first ingestion of the S&P 500 universe (SPY holdings of
2026-10-01) and first live evaluation of L (`l-input-contract-v1`, L Score v1
`l-1.0-exp`, weights of spec §8, approved 2026-10-03) on the 26 validation
companies. Reproduce with `python -m database.universe_v1` and
`python -m database.batch_v3 <tickers> [--ingest]`.

## Universe

- 506 holdings lines: 504 equities, 2 non-equity lines skipped.
- 502 members ingested (profile with SIC code + daily bars) plus SPY; no
  member unresolved, no bars download failed.
- One member per issuer: GOOG, FOX and NWS skipped as second share classes of
  GOOGL, FOXA and NWSA (the first run crashed on GOOG/GOOGL sharing a CIK;
  fixed, and one member can no longer stop the run).
- Distribution at as_of 2026-10-03T21:09Z: **497 members scored**, 7 excluded
  (the 3 share classes and 4 listings with less than a year of history: HONA,
  VYLR, Q, FDXF). No member was excluded for a suspected split.
- Largest SIC major groups: 73 (business services/software, 55), 38
  (instruments, 40), 49 (utilities, 37), 28 (chemicals/pharma, 34), 35
  (computers/machinery, 33), 36 (electronics/semiconductors, 31).

## Results

| Ticker | RS | L Score | Classic | Group (rank %) | Integrity |
|---|---|---|---|---|---|
| TSM | 90 | 82.14 | PASS | 36 (89.5) | REVIEW (foreign) |
| ASML | 92 | 79.09 | PASS | 35 (92.1) | REVIEW (foreign) |
| LRCX | 95 | 76.92 | PASS | 35 (92.1) | VERIFIED |
| NVDA | 86 | 75.98 | PASS | 36 (89.5) | VERIFIED |
| SMCI | 94 | 75.28 | PASS | 35 (92.1) | VERIFIED |
| AAPL | 82 | 66.13 | PASS | 35 (92.1) | VERIFIED |
| XOM | 87 | 65.20 | PASS | 29 (100) | VERIFIED |
| MSFT | 85 | 62.34 | PASS | 73 (63.2) | VERIFIED |
| PLTR | 86 | 61.78 | PASS | 73 (63.2) | VERIFIED |
| JPM | 58 | 29.89 | FAIL_LAGGARD | 60 (57.9) | VERIFIED |
| AVGO | 53 | 24.12 | FAIL_LAGGARD | 36 (89.5) | VERIFIED |
| BRK-B | 51 | 17.96 | FAIL_LAGGARD | 63 (60.5) | VERIFIED |
| CRWV | 51 | 16.44 | FAIL_LAGGARD | 73 (63.2) | VERIFIED |
| SNAP | 48 | 15.29 | FAIL_LAGGARD | 73 (63.2) | VERIFIED |
| BAC | 45 | 14.42 | FAIL_LAGGARD | 60 (57.9) | VERIFIED |
| UBER | 20 | 13.88 | FAIL_LAGGARD | 73 (63.2) | VERIFIED |
| ORCL | 13 | 13.14 | FAIL_LAGGARD | 73 (63.2) | VERIFIED |
| RDDT | 8 | 12.69 | FAIL_LAGGARD | 73 (63.2) | VERIFIED |
| PLD | 45 | 11.55 | FAIL_LAGGARD | 67 (31.6) | VERIFIED |
| COST | 44 | 9.58 | FAIL_LAGGARD | 53 (47.4) | VERIFIED |
| WMT | 32 | 7.58 | FAIL_LAGGARD | 53 (47.4) | VERIFIED |
| O | 22 | 7.13 | FAIL_LAGGARD | 67 (31.6) | VERIFIED |
| CMG | 29 | 6.95 | FAIL_LAGGARD | 58 (18.4) | VERIFIED |
| NEE | 25 | 5.88 | FAIL_LAGGARD | 49 (13.2) | VERIFIED |
| RIVN | 10 | 4.36 | FAIL_LAGGARD | 37 (23.7) | VERIFIED |
| LCID | 1 | 3.79 | FAIL_LAGGARD | 37 (23.7) | VERIFIED |

Summary: integrity VERIFIED 24, REVIEW 2 (foreign filers); classic PASS 9,
FAIL_LAGGARD 17.

## Checks

- NVDA recomputed independently from `yfinance` (2-year history, same day):
  weighted score 1.243095, RS 86 against the stored distribution — identical.
- C, A and S are identical to the previous run for all 26 companies.
- RS is consistent with N: the strongest RS (LRCX, SMCI, NVDA, XOM) are near
  their 52-week highs; the weakest (LCID, RDDT, ORCL) are far below.
- Known bias (spec §5): today's constituents are used for every `as_of`.
