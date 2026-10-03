# Validation of C and A on 26 Companies

**Date:** 2026-10-02

**Scope:** fundamentals v3, C v3 + C Score v1.3, and A v1, run against real
SEC and Yahoo evidence for companies chosen to stress the difficult cases.
Reproduce with `python -m database.batch_v3 <tickers>`.

| Profile | Tickers |
|---|---|
| Large, clean | AAPL, MSFT, NVDA |
| Losses | RIVN, SNAP, LCID |
| Loss to profit | PLTR, UBER, RDDT |
| Banks | JPM, BAC |
| REITs | O, PLD |
| Insurance / multi-class | BRK-B |
| Foreign filers (20-F) | TSM, ASML |
| Recent IPO | CRWV |
| Recent splits | AVGO (10:1), CMG (50:1), WMT (3:1), LRCX (10:1), SMCI (10:1) |
| Non-calendar fiscal years | COST, ORCL, WMT, NVDA, MSFT, AAPL |
| Other sectors | XOM, NEE |

## Defects found and fixed

| # | Finding | Fix |
|---|---|---|
| 1 | Revenue catalog preferred ASC 606 contract revenue, a subset, over total `Revenues` (BRK: 70.1B vs 101.8B). Banks (`RevenuesNetOfInterestExpense`) and utilities (`RegulatedAndUnregulatedOperatingRevenue`) were missing. | `sec-tag-catalog-v5`: totals first, bank and utility tags added. |
| 2 | ROE stopped at the first concept pair when its equity was missing (RIVN). | All consistent pairs are tried; pairs are never mixed. |
| 3 | yfinance lists a 1.032 "split" for O (Orion spin-off price adjustment), and it was applied to EPS. | Provider-only events are verified against SEC share counts; events SEC contradicts are rejected and reported. |
| 4 | A pre-IPO split reported by SEC (CRWV) forced review. | A SEC split before the first periodic filing is informational. |
| 5 | Large split ratios (7, 25, 50…) could not be detected as undeclared splits. | Ratio list extended (CMG 50:1). |
| 6 | SEC's Company Facts API lags EDGAR: PLD and NEE July 10-Qs were public but absent from the API, so C used Yahoo. | `sec-xbrl-instance-v1`: the filing's own XBRL instance is read (parity verified: AAPL 26/26, PLD 18/18 identical facts). |
| 7 | XOM moved to a new registrant (8-K12B, 2026-07-01); its history stayed under the old CIK. | Automatic succession detection (succession form + jointly filed reports + predecessor 10-K history); history merged and the succession reported in C and A. Successions outside the seven-year evidence window are informational. |
| 8 | Foreign filers looked like missing data. | Explicit `FOREIGN_FILER_NOT_SUPPORTED`. |
| 9 | A reported `ProfitLoss` (includes minority interest) outranked an exactly derived `NetIncomeLoss` (PLD Q4). | Concept preference first, then reported before derived. |
| 10 | `ingestion_runs` accepted FAILED rows without an error code (NULL in a CHECK passes). | Constraint fixed; found by the new PostgreSQL integration tests. |

## Correct outcomes worth noting

- Loss-making companies score low (C 24–32) and fail A on loss years, instead
  of scoring high on sales alone (the C v1.2 defect).
- JPM Q2 2026 revenue differs between SEC (57.35B) and Yahoo (52.85B) while
  the four prior quarters match to the dollar; C goes to review.
- O reports `Revenues` 5–8% above Yahoo every quarter and has an internal Q4
  inconsistency; C goes to review.

## Known limitations (decided, not defects)

- **Foreign private issuers** (20-F/40-F) are not analyzed (human decision).
- **Multi-class EPS** (BRK): SEC EPS is per Class A share; the B ticker has no
  comparable SEC EPS. C uses Yahoo and goes to review; A has no annual EPS.
- **Q4 EPS** is not derived (human decision: official data only). Around a Q4,
  the previous YoY can be unavailable when Yahoo's five quarters do not cover
  the comparable (ORCL); C is then partial and goes to review.
- **Loss to profit** keeps EPS growth unavailable and sends the score to
  review (C v1.3 and A v1).
