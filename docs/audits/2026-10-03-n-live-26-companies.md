# N v1 Live Run on 26 Companies

**Date:** 2026-10-03

**Scope:** first ingestion of daily price bars and 8-K items (migration
`2026-10-03_price_bars_v1`, applied 2026-10-03 with explicit authorization)
and the first live evaluation of N v1 + N Score v1 (`n-1.0-exp`) on the 26
validation companies of `2026-10-02-validation-26-companies.md`.
Reproduce with `python -m database.batch_v3 <tickers> [--ingest]`.

## Evidence stored

- `yahoo_price_bars_raw`: 36,883 bars, 26 companies, 2020-10-05..2026-10-02.
  No partial session bars and no rejected rows in any run.
- `sec_filing_items`: 13,567 literal items, all 26 companies (the six of
  finding 2 after its fix).
- PostgreSQL integration tests (11, rolled back) green after the migration.

## Results (as_of 2026-10-03T17:44:55Z)

| Ticker | % below 52w high | N Score | Classic | Integrity |
|---|---|---|---|---|
| NVDA | 1.7 | 97.52 | PASS | VERIFIED |
| AAPL | 3.4 | 94.94 | PASS | VERIFIED |
| BRK-B | 6.5 | 75.31 | PASS | VERIFIED |
| TSM | 1.3 | 73.05 | PASS | REVIEW_REQUIRED (foreign filer) |
| JPM | 9.3 | 68.73 | PASS | VERIFIED |
| MSFT | 6.5 | 63.51 | PASS | VERIFIED |
| ASML | 6.6 | 63.42 | PASS | REVIEW_REQUIRED (foreign filer) |
| XOM | 7.0 | 62.43 | PASS | VERIFIED |
| PLTR | 9.0 | 57.39 | PASS | VERIFIED |
| PLD | 15.9 | 43.82 | FAIL_FAR_FROM_HIGH | VERIFIED |
| BAC | 17.6 | 43.76 | FAIL_FAR_FROM_HIGH | VERIFIED |
| COST | 16.0 | 35.51 | FAIL_FAR_FROM_HIGH | VERIFIED |
| LRCX | 20.8 | 24.19 | FAIL_FAR_FROM_HIGH | VERIFIED |
| O | 20.3 | 22.49 | FAIL_FAR_FROM_HIGH | VERIFIED |
| NEE | 22.2 | 20.73 | FAIL_FAR_FROM_HIGH | VERIFIED |
| WMT | 22.9 | 19.13 | FAIL_FAR_FROM_HIGH | VERIFIED |
| CMG | 24.4 | 11.14 | FAIL_FAR_FROM_HIGH | VERIFIED |
| AVGO | 28.3 | 9.88 | FAIL_FAR_FROM_HIGH | VERIFIED |
| SMCI | 25.7 | 9.33 | FAIL_FAR_FROM_HIGH | VERIFIED |
| UBER | 32.8 | 4.05 | FAIL_FAR_FROM_HIGH | VERIFIED |
| SNAP | 38.9 | 0.00 | FAIL_FAR_FROM_HIGH | VERIFIED |
| ORCL | 55.9 | 0.00 | FAIL_FAR_FROM_HIGH | VERIFIED |
| LCID | 83.6 | 0.00 | FAIL_FAR_FROM_HIGH | VERIFIED |
| RIVN | 37.0 | 0.00 | FAIL_FAR_FROM_HIGH | PARTIAL (no 5-year window) |
| RDDT | 43.9 | 0.00 | FAIL_FAR_FROM_HIGH | PARTIAL (no 5-year window) |
| CRWV | 41.5 | 0.00 | FAIL_FAR_FROM_HIGH | PARTIAL (no 5-year window) |

- Cross-check against `yfinance` directly (1-year history, same day): NVDA
  1.7, ORCL 55.9, LCID 83.6, TSM 1.3, AAPL 3.4 — identical.
- Recent new 52-week highs (last 20 sessions): AAPL, NVDA.
- Splits inside the window (AVGO, CMG, WMT, LRCX, SMCI) needed no factor:
  every bar comes from today's observation, already on today's basis.
- Catalysts: 5.02 events in 17 companies (LCID 9), one 2.01 (XOM).

## Findings

| # | Finding | Status |
|---|---|---|
| 1 | `batch_v3 --ingest` fixed `as_of` before ingesting, so point-in-time correctly hid every bar just observed: N was `NO_PRICE_EVIDENCE` for all 26 on the first run. | Fixed: ingestion runs first for every ticker; without `--as-of` the evaluation time is taken afterwards. Tests added. |
| 2 | SEC submissions serve inconsistent `acceptanceDateTime` values: for six companies (BAC, CRWV, JPM, PLD, RDDT, RIVN) today's values differ from yesterday's by exactly the New York UTC offset (4 h EDT, 5 h EST). The `sec.submissions` runs failed closed with `EVIDENCE_CONFLICT`, so their new filings and 8-K items were not stored. | Fixed per human decision (spec §14): acceptance observations, catalysts use the latest. |

### Finding 2 detail

Checked against the "Accepted" field of the EDGAR filing index pages (New
York time):

| Filing | EDGAR (NY) | Stored 2026-10-02 | Served 2026-10-03 |
|---|---|---|---|
| BAC 0001918704-26-030102 | 09:18:26 | 13:18 UTC (correct) | 17:18 UTC (wrong) |
| BAC 0001918704-26-030026 | 17:32:58 | 21:32 UTC (correct) | 01:32 UTC (wrong) |
| CRWV 0001769628-20-000002 | 17:25:22 | 03:25 UTC (wrong) | 22:25 UTC (correct) |
| CRWV 0001769628-19-000001 | 10:34:55 | 18:34 UTC (wrong) | 14:34 UTC (correct) |

- The wrong value is always the true time **plus** the New York offset, as if
  a UTC time were converted from New York a second time. The "Z is real UTC"
  verification of 2026-10-02 (1,000 NVDA filings) holds most of the time but
  not always. CRWV: 1,140 stored filings affected; BAC: 32 (all filed
  2026-10-02).
- Impact is bounded: only N catalysts read `acceptance_at` (C and A use
  `observed_at`). A wrong value is always later than the truth, so it can
  delay a catalyst but never leak one into an earlier `as_of`.
- Practical harm: while the conflict stood, the six companies got no new
  filing metadata, which also feeds lagging-filing detection and the filer
  classification.

### Resolution

Human decision (option 1): keep every observation, never fail on it, never
infer the truth. Migration `2026-10-03_filing_acceptance_observations_v1`
(applied) seeded 268,523 first observations from `sec_filings`; ingestion
`sec-submissions-v3` records changed values; catalysts `sec-catalysts-v2`
use the latest value observed by `as_of` and flag `ACCEPTANCE_INCONSISTENT`.
Re-ingesting the six companies succeeded; 5,390 filings now carry two
distinct acceptance values. N results are unchanged; their catalysts now
appear (BAC 1, JPM 3, PLD 5, RDDT 1, RIVN 3 Item 5.02 events; CRWV none).
