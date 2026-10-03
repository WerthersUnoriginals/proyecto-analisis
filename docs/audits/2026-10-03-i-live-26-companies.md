# I v1 Live Run on 26 Companies

**Date:** 2026-10-03

**Scope:** first ingestion of SEC Form 13F data sets and first live
evaluation of I (`i-input-contract-v1`, I Score v1 `i-1.0-exp`, weights of
spec §8 approved 2026-10-03 with a 1% tolerance in the classic rule) on the 26 validation companies. Reproduce
with `python -m database.institutional_v1` and
`python -m database.batch_v3 <tickers>`.

## Evidence stored

| Data set (filing window) | New filings | Tracked holdings |
|---|---|---|
| 01jun2025-31aug2025 | 8,811 | 1,194,046 |
| 01sep2025-30nov2025 | 8,570 | 1,122,875 |
| 01dec2025-28feb2026 | 9,364 | 1,207,266 |
| 01mar2026-31may2026 | 9,716 | 1,291,664 |
| 01jun2026-31aug2026 | 9,900 | 1,321,763 |

Quarters: 2025-06-30 to 2026-06-30. CUSIPs: 500 official (SPY file), 7 from
name match (ASML, CRWV, LCID, RIVN, SNAP, TSM and SPY itself), 1 disagreement
(OKE: SPY 30609A109 vs 13F filers 682680103 → review).

## Results (as_of 2026-10-03T21:49Z)

| Ticker | Holders Q2-26 | QoQ % | Ownership % | I Score | Classic | Integrity |
|---|---|---|---|---|---|---|
| LRCX | 3,101 | +18.6 | 85.2 | 99.89 | PASS | VERIFIED |
| TSM | 3,560 | +8.6 | — | 98.44 | PASS | REVIEW (foreign) |
| ASML | 2,501 | +13.7 | — | 100.00 | PASS | REVIEW (foreign) |
| CRWV | 967 | +23.7 | 91.5 | 96.13 | PASS | VERIFIED (name match) |
| RDDT | 892 | +12.2 | 62.6 | 90.00 | PASS | VERIFIED |
| PLD | 1,733 | +3.3 | 92.3 | 87.15 | PASS | VERIFIED |
| NVDA | 5,957 | +1.6 | 68.5 | 86.05 | PASS | VERIFIED |
| O | 1,457 | +0.9 | 82.0 | 83.59 | PASS | VERIFIED |
| AVGO | 4,823 | +2.9 | 76.6 | 81.16 | PASS | VERIFIED |
| BAC | 3,605 | +3.5 | 69.9 | 78.53 | PASS | VERIFIED |
| RIVN | 817 | +1.0 | 62.7 | 78.02 | PASS | VERIFIED (name match) |
| JPM | 5,126 | +0.8 | 70.8 | 70.19 | PASS | VERIFIED |
| SMCI | 767 | +8.8 | 64.6 | 67.23 | PASS | VERIFIED |
| BRK-B | 4,901 | +0.7 | — | 55.16 | PASS | PARTIAL (no share count) |
| ORCL | 3,515 | +0.5 | 44.6 | 51.40 | PASS | VERIFIED |
| NEE | 3,034 | −0.4 | 85.4 | 63.54 | PASS (tolerance) | VERIFIED |
| AAPL | 6,111 | −0.02 | 64.6 | 63.20 | PASS (tolerance) | VERIFIED |
| MSFT | 6,213 | −0.1 | 73.8 | 60.49 | PASS (tolerance) | VERIFIED |
| COST | 4,232 | −0.7 | 72.7 | 57.93 | PASS (tolerance) | VERIFIED |
| UBER | 2,425 | −0.8 | 79.6 | 52.86 | PASS (tolerance) | VERIFIED |
| WMT | 4,397 | −1.3 | 37.0 | 51.41 | FAIL_DECLINING | VERIFIED |
| PLTR | 2,841 | −5.2 | 56.5 | 50.73 | FAIL_DECLINING | VERIFIED |
| SNAP | 533 | −2.7 | 37.4 | 34.18 | FAIL_DECLINING | VERIFIED (name match) |
| CMG | 1,190 | −4.6 | 85.6 | 30.49 | FAIL_DECLINING | VERIFIED |
| LCID | 370 | +5.1 | 97.5 | 78.09 | PASS | REVIEW (discontinuity) |
| XOM | 398 | — | 1.2 | 67.61 | PASS | REVIEW (discontinuity) |

Summary: integrity VERIFIED 21, REVIEW 4, PARTIAL 1; classic (approved 1%
tolerance) PASS 22, FAIL_DECLINING_SPONSORSHIP 4. C, A, N, S and L unchanged.

## Checks

- Holder counts agree with the direct probe of the latest data set
  (NVDA 5,957 vs 5,928 distinct filers in one file; AAPL 6,111 vs 6,134):
  the stored count applies restatements and includes late filings from
  other windows.
- With the strict rule as first proposed, large caps failed on tiny declines
  (AAPL −0.02%, MSFT −0.1%). The human approved a 1% tolerance: with it NEE,
  AAPL, MSFT, COST and UBER pass, and the classic count becomes PASS 22,
  FAIL_DECLINING_SPONSORSHIP 4 (WMT, PLTR, SNAP, CMG).

## Findings

| # | Finding | Status |
|---|---|---|
| 1 | Older 13F data sets keep their tables inside a folder; the parser expected them at the root and the run stopped on the first file (a Windows file lock hid the real error). | Fixed: tables found by name at any depth, archives always closed, one unreadable data set no longer stops the others. |
| 2 | XOM's July 2026 reorganization gave it a new CUSIP: 4 holders at Q1, 398 at Q2 (+9,850%), 1.2% ownership. LCID's September 2025 reverse split also changed its CUSIP (1 holder at Q2 2025, 321 at Q3). | Fixed: a count that doubles or halves between quarters (with at least 20 holders) is `HOLDERS_DISCONTINUITY` → review. Combining the old and new CUSIPs is deferred. |
| 3 | `insert_company_profile` compared with the latest profile at any time instead of the latest observed by then (found by the PostgreSQL integration test). | Fixed. |
