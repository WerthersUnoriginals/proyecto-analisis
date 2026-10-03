# S v1 Live Run on 26 Companies

**Date:** 2026-10-03

**Scope:** first live evaluation of S (`s-input-contract-v1`, S Score v1
`s-1.0-exp`, weights of spec §8, approved 2026-10-03) on the 26 validation
companies, after re-ingesting SEC Company Facts with `sec-tag-catalog-v7`
(debt tags and basic shares). Reproduce with
`python -m database.batch_v3 <tickers> [--ingest]`.

## Results (as_of 2026-10-03T18:28:57Z)

| Ticker | Shares 3y % | D/E | D/E source | Up/down vol 50d | S Score | Classic | Integrity |
|---|---|---|---|---|---|---|---|
| JPM | −6.3 | — | none (bank) | 1.29 | 85.55 | PASS | PARTIAL |
| MSFT | −0.3 | 0.09 | LongTermDebt | 1.86 | 84.42 | PASS | VERIFIED |
| TSM | — | — | — | 1.39 | 83.94 | INSUFFICIENT_DATA | REVIEW (foreign) |
| NVDA | −2.2 | 0.05 | LongTermDebt | 1.24 | 81.30 | PASS | VERIFIED |
| BRK-B | — | — | — | 1.32 | 79.30 | INSUFFICIENT_DATA | REVIEW (no share tags) |
| CMG | −4.3 | 0.00 | LongTermDebt (0) | 1.13 | 77.89 | PASS | VERIFIED |
| LRCX | −7.2 | 0.30 | LongTermDebt | 1.01 | 75.65 | PASS | VERIFIED |
| COST | 0.0 | 0.20 | noncurrent+current | 1.12 | 69.85 | PASS | VERIFIED |
| AAPL | −8.1 | 1.23 | LongTermDebt | 1.05 | 69.59 | PASS | VERIFIED |
| XOM | +2.4 | 0.16 | lease-inclusive | 1.13 | 65.04 | PASS | VERIFIED |
| BAC | −6.0 | 1.05 | LongTermDebt | 1.03 | 64.18 | PASS | VERIFIED |
| WMT | −2.2 | 0.38 | noncurrent+current | 0.88 | 57.50 | FAIL_DISTRIBUTION | VERIFIED |
| SMCI | +24.6 | 0.28 | | 1.72 | 51.24 | FAIL_DILUTION | VERIFIED |
| UBER | +7.3 | 0.39 | | 0.87 | 44.40 | FAIL_DISTRIBUTION | VERIFIED |
| SNAP | +5.4 | 1.55 | | 1.16 | 44.28 | FAIL_DILUTION | VERIFIED |
| ORCL | +5.4 | 3.05 | | 1.08 | 43.23 | FAIL_DILUTION | VERIFIED |
| PLTR | +24.3 | — | none | 1.37 | 40.26 | FAIL_DILUTION | PARTIAL |
| ASML | — | — | — | 0.97 | 36.96 | INSUFFICIENT_DATA | REVIEW (foreign) |
| CRWV | — | 6.41 | | 1.02 | 26.86 | INSUFFICIENT_DATA | PARTIAL (IPO 2025) |
| NEE | +4.6 | 1.70 | | 0.69 | 23.33 | FAIL_DISTRIBUTION | VERIFIED |
| AVGO | +14.7 | 0.80 | | 0.87 | 22.99 | FAIL_DISTRIBUTION | VERIFIED |
| PLD | +17.9 | 0.66 | | 0.62 | 19.30 | FAIL_DISTRIBUTION | VERIFIED |
| RIVN | +29.9 | 0.48 | older year | 0.63 | 11.71 | FAIL_DISTRIBUTION | VERIFIED |
| LCID | +85.1 | 3.79 | | 0.84 | 6.20 | FAIL_DISTRIBUTION | VERIFIED |
| O | +48.4 | — | none (REIT) | 0.39 | 1.12 | FAIL_DISTRIBUTION | PARTIAL |
| RDDT | +253.0 | — | none | 0.53 | 0.00 | FAIL_DISTRIBUTION | PARTIAL |

Summary: integrity VERIFIED 18, PARTIAL 5, REVIEW 3; classic PASS 9,
FAIL_DISTRIBUTION 9, FAIL_DILUTION 4, INSUFFICIENT_DATA 4.

## Checks

- C, A and N are identical to the previous run for all 26 companies after
  the catalog change (the annual view defaults and split verification are
  untouched).
- Large share changes match known corporate events: AVGO (VMware shares,
  2023), O (Spirit merger), LCID (equity raises), RDDT (preferred converted at
  the March 2024 IPO).
- AAPL D/E 1.23: long-term debt about 1.2× its buyback-reduced equity, as
  expected.

## Findings

| # | Finding | Status |
|---|---|---|
| 1 | XOM tags only basic weighted shares since 2013; S had no share data. | Fixed: basic-shares fallback, catalog v7 (spec §9). |
| 2 | Banks and REITs (JPM, O) report debt under other tags: D/E unavailable, score renormalized and flagged. BAC's D/E is computed but not comparable to industrials. | Known limitation (spec §5); sector data deferred. |
| 3 | BRK-B reports no standard share-count tags. | Reported as review; no fix. |
