# S — Supply and Demand Design (v1)

**Status:** Approved by the human on 2026-10-03, including the S Score v1
weights and classic thresholds (§8). Implemented; live run in
`docs/audits/2026-10-03-s-live-26-companies.md`.

**Date:** 2026-10-03

**Builds on:** fundamentals v3 (raw evidence, fiscal calendar,
`split-basis-v1`), the annual view of A (`sec-annual-v1`), and the N price
series (`price-series-v1`).

## 1. Human decisions (2026-10-03)

- **H1 — Scope.** S covers every O'Neil supply-and-demand criterion that fits
  the system:
  - scored: **share supply** (buybacks vs dilution), **leverage** (debt to
    equity and its trend), **demand** (volume on up days vs down days);
  - informative, unscored: **excessive splits** and **size**.
- **H2 — Out of S.** Management ownership is deferred (Forms 3/4/5 and proxy
  parsing). Breakout volume belongs to the technical layer (it needs base
  detection).

## 2. Evidence

- **Share counts:** `DILUTED_SHARES` (weighted average diluted shares), already
  in the catalog, from 10-K annual periods, converted to the `as_of` split
  basis like EPS but inversely (`split-basis-v1`).
- **Debt:** catalog `sec-tag-catalog-v6` adds the `DEBT` metric (unit USD) with
  `LongTermDebt`, `LongTermDebtNoncurrent`, `LongTermDebtCurrent`,
  `LongTermDebtAndCapitalLeaseObligations`,
  `LongTermDebtAndCapitalLeaseObligationsCurrent` and
  `DebtLongtermAndShorttermCombinedAmount`. A re-ingestion adds the new tags
  without touching existing rows.
- **Equity:** `STOCKHOLDERS_EQUITY`, as in A.
- **Volume:** the N price series (adjusted to the `as_of` basis).

Survey of the 26 validation companies (2026-10-03): debt is reported under
very different tags (AAPL `LongTermDebt`; COST/WMT/NEE noncurrent + current;
XOM lease-inclusive; ORCL combined); BRK-B, JPM, O and the foreign filers
report none of them recently; PLTR and RDDT report none (likely no debt, but
absence is not evidence of zero).

## 3. Annual view extension (no change for A)

`build_annual_view` gains `metrics` and `instant_metrics` parameters whose
defaults are today's (`EPS_DILUTED, REVENUE, NET_INCOME` and
`STOCKHOLDERS_EQUITY`), so A is unchanged. S calls it with
`metrics=("DILUTED_SHARES",)` and
`instant_metrics=("STOCKHOLDERS_EQUITY", "DEBT")`; share-count diagnostics
therefore never reach A.

## 4. Calculations `s-supply-demand-v1`

**Share supply** (fiscal year `N` = latest with diluted shares):

- `shares_change_1y_pct`: year `N` vs `N−1`, same concept (annual growth rule
  of A).
- `shares_change_3y_pct`: total change `N` vs `N−3`, same concept, anchored at
  `N`, no older-window substitution. Positive = dilution, negative = buyback.

**Leverage** at fiscal year ends, debt definitions tried in order, never
mixed:

1. `LongTermDebt`;
2. `LongTermDebtNoncurrent + LongTermDebtCurrent` (both on the same date);
3. `LongTermDebtAndCapitalLeaseObligations +
   LongTermDebtAndCapitalLeaseObligationsCurrent`;
4. `DebtLongtermAndShorttermCombinedAmount`.

A noncurrent amount without its current part is not used (it would
understate debt). Equity: `StockholdersEquity`, else the variant including
noncontrolling interest.

- `debt_to_equity_latest` at the end of year `N_d` (latest year with both
  debt and equity); `debt_to_equity_3y_ago` at `N_d−3` with the **same** debt
  definition and equity concept; `debt_to_equity_change` = latest − 3y ago.
- Equity ≤ 0 → `NOT_MEANINGFUL` (unavailable, review), never scored as zero.
- No definition available → `NO_DEBT_EVIDENCE` (unavailable): absence is
  never read as zero debt.

**Demand:** over the last 50 sessions of the price series (51 bars needed),
`up_down_volume_ratio_50d` = volume on sessions closing above the previous
close ÷ volume on sessions closing below it (unchanged sessions ignored).
Fewer than 51 bars → `INSUFFICIENT_HISTORY`; no down volume →
`NO_DOWN_VOLUME` (unavailable); stale prices as in N (more than 5 business
days) → review.

**Informative:** split events in the reconciled window (date, ratio) and
their count; approximate market value = last close × latest diluted shares
(both on the `as_of` basis), labelled approximate because weighted diluted
shares are not shares outstanding.

## 5. Integrity

- Foreign filer, registrant history needing review, or split status
  `REVIEW_REQUIRED/UNKNOWN/UNADJUSTED_DETECTED` → `REVIEW_REQUIRED` (as A).
- Latest fiscal year older than 455 days → `REVIEW_REQUIRED`.
- Non-meaningful equity or stale/inconsistent prices → `REVIEW_REQUIRED`.
- Any core value unavailable → `VERIFIED_WITH_PARTIAL_CORE_DATA`.
- Known limitation: debt to equity of banks and insurers is not comparable
  to industrial companies; no sector data is stored yet, so it is reported as
  is and listed here.

## 6. Contract `s-input-contract-v1`

`latest_fiscal_year`, `diluted_shares_latest`, `shares_change_1y_pct`,
`shares_change_3y_pct`, `debt_to_equity_latest`, `debt_to_equity_3y_ago`,
`debt_to_equity_change`, `debt_definition`, `up_down_volume_ratio_50d`,
`volume_sessions`, `split_events`, `market_value_approx`, `s_data_integrity`,
`split_integrity_status`; provenance and reasons per input as in A.

## 7. Delivery

Spec → catalog v6/v7 + annual view parameters → calculations → contract → score
(after weights approval) → runner and batch → re-ingestion of the 26
companies (new tags) → live run and audit note.

## 8. S Score v1 (approved 2026-10-03)

**Graded score (100 points, renormalized over available components):**

| Component | Max | Curve (input → points) |
|---|---|---|
| Shares change, 3 years (%) | 30 | −10→30, −5→26, 0→20, +5→10, +10→4, +20→0 |
| Shares change, 1 year (%) | 10 | −3→10, 0→7, +3→3, +6→0 |
| Debt to equity, latest | 15 | 0→15, 0.3→13, 0.6→9, 1.0→5, 2.0→0 |
| Debt to equity change, 3 years | 10 | −0.3→10, 0→7, +0.3→2, +0.6→0 |
| Up/down volume ratio, 50 sessions | 35 | 0.7→0, 0.9→8, 1.0→15, 1.2→25, 1.5→32, 2.0→35 |

**Classic O'Neil result**, in order:

1. `INSUFFICIENT_DATA` when the volume ratio or the 3-year share change is
   unavailable;
2. `FAIL_DISTRIBUTION` when the volume ratio is below 1.0;
3. `FAIL_DILUTION` when shares grew more than 5% in 3 years;
4. `FAIL_DEBT_RISING` when debt to equity rose more than 0.25 in 3 years;
5. `PASS` otherwise.

Status and usability follow A and N (`REVIEW_REQUIRED` integrity →
`REVIEW_REQUIRED_DATA`; fewer than 80 available points → `PARTIAL_SCORE`).
Splits and size are flags only.

## 9. Implementation notes from real data (2026-10-03)

- **Basic shares fallback (catalog `sec-tag-catalog-v7`).** XOM stopped tagging
  diluted weighted shares in 2013 and tags only basic ones (equal for it).
  `BASIC_SHARES` (`WeightedAverageNumberOfSharesOutstandingBasic`) is used when
  it is strictly more recent than diluted shares; the chosen concept is
  reported and the 3-year change never mixes concepts. Basic shares are split
  adjusted like diluted ones but never used to verify provider splits, so C
  and A are unchanged. Catalog v6 was used by one real ingestion before this
  addition, so the change takes a new version name.
- **IPO conversions.** Weighted shares before an IPO exclude preferred stock
  converted at the IPO (RDDT: 59M in 2023, 202M in 2025, +253% over three
  years). This is a real increase in common shares and is reported as such.
- **Leverage from an older year** (RIVN: the current part of long-term debt was
  last tagged in 2024) is flagged `LEVERAGE_NOT_LATEST_FISCAL_YEAR`.
