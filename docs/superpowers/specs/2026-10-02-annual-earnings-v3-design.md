# A — Annual Earnings Design (on fundamentals v3)

**Status:** Implemented 2026-10-02; A Score v1 weights approved by the human.
Supersedes
`2026-09-28-annual-earnings-design.md`, which is kept as history.

**Date:** 2026-10-02

**Builds on:** `2026-10-02-fundamentals-v3-design.md` (raw evidence, fiscal
calendar, split basis, PIT) and the C score conventions.

## 1. Human decisions (2026-10-02)

- **H1 — Scope.** A measures annual EPS (YoY, 3- and 5-year CAGR, down and loss
  years), ROE, and annual sales as confirmation.
- **H2 — ROE.** Net income of the fiscal year divided by the average of
  opening and closing stockholders' equity. If either equity value is zero or
  negative, ROE is not meaningful: it is unavailable, flagged, and sent to
  review. It is never computed and never scored as zero.
- **H3 — Score.** A pre-score data contract plus A Score v1: a classic O'Neil
  result and a graded 0–100 score. The weights in §8 are a proposal awaiting
  human approval before the score is implemented.
- **H4 — Codex draft.** Kept as superseded history.

## 2. What the Codex draft needed that v3 already provides

| Draft requirement | v3 status |
|---|---|
| `period_type` column, nullable rollout, and backfill (§5–6) | Not needed: v3 classifies durations on read; nothing is persisted per interpretation. |
| Raw identity with `period_start`, Q4/FY coexistence (§7) | Done: `sec_companyfacts_raw` identity includes `period_start` (`NULLS NOT DISTINCT`). |
| Annual acquisition (A1.2) | Done: annual facts are already stored for every catalog tag. |
| FY identity from original presentation (§8.3) | Replaced: SEC `fy` is unreliable (NVDA). v3 uses real fiscal year-end dates. |
| Resolution lineage and `identity_available_at` (§11) | Simplified: normalization on read from evidence with `observed_at <= as_of` cannot use later knowledge. |
| Capability gates and writer inventories (§6.1) | Dropped: disproportionate for a single-writer local installation. |
| Latest filing per FY, no split handling (§8.6, §2.2) | **Corrected:** a 10-K restates only three years, so A applies `split-basis-v1`. Without it, NVDA FY2023 YoY would be −95.6% instead of about −55%. |

Kept from the draft: fail-closed statuses, single-source and single-concept
arithmetic, CAGR anchored at the latest fiscal year with no older-window
substitution, explicit null reasons, and separate namespaces for quality,
calculation, and usability.

## 3. Evidence

- Catalog `sec-tag-catalog-v4` = v3 plus `STOCKHOLDERS_EQUITY`:
  `StockholdersEquity`, then
  `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`
  (instant facts, unit USD). Raw rows record their catalog version, so
  re-ingestion adds the new tags without touching existing rows.
- Only SEC evidence is used. Yahoo annual data is deferred (decision 5 of the
  Codex draft stands): SEC covers US issuers, and mixing sources is forbidden
  anyway.

## 4. Annual normalization `sec-annual-v1`

Input: SEC facts with `observed_at <= as_of` and `filed_date <= as_of`.

- **Fiscal years:** the non-projected years of `build_fiscal_calendar` (real
  10-K year-end dates). Labels as in v3.
- **Annual values (EPS, revenue, net income):** facts in `10-K`/`10-K/A` forms
  whose interval is classified `ANNUAL` and ends on a fiscal year end. Per
  metric, tag, and fiscal year, the latest visible filing wins. Values are
  converted to the `as_of` share basis for EPS, and restated pairs are checked
  for undeclared splits with the same rules as quarterly (`split-basis-v1`).
  An interval that does not end on a fiscal year end is ignored.
- **Equity:** instant `STOCKHOLDERS_EQUITY` facts dated exactly on a fiscal
  year end; latest visible filing per tag and date. Equity is not converted by
  splits (it is a monetary amount).
- **Analysis window:** values back to seven years before `as_of`; split checks
  and events within six years, as in the split capture.

## 5. Calculations (single source, single concept)

For fiscal year `N` = the latest fiscal year with an EPS value:

- `annual_eps_series`: last six fiscal years with value, tag, filing, basis
  factor, and lineage.
- YoY for year `y`: the same tag in `y` and `y−1`, with `y−1 > 0`. Status per
  year: `GROWTH`, `LOSS` (current ≤ 0), `LOSS_TO_PROFIT` (prior ≤ 0 < current),
  or `NO_DATA`.
- `eps_cagr_3y`: years `N−3` and `N` with one tag present in all of
  `N−3..N`; both endpoints > 0; otherwise `None` with reason
  (`INSUFFICIENT_HISTORY`, `NON_POSITIVE_ENDPOINT`, `NO_COMMON_CONCEPT`). Never
  an older window. `eps_cagr_5y` is the same over `N−5..N`.
- `down_years` and `loss_years` over the last three transitions and years
  (`N−2..N` against their priors).
- `roe_latest` for year `N` (H2), using the net-income tag paired with its
  equity tag: `NetIncomeLoss` ↔ `StockholdersEquity`, `ProfitLoss` ↔
  `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`.
  Pairs are never mixed. `roe_3y_avg` is the mean of the ROE of `N−2..N` when
  all three are meaningful.
- `sales_cagr_3y` and the latest sales YoY: same rules as EPS on REVENUE.

## 6. Integrity

- **Split status:** the same reconciliation and view checks as C v3.
- **Stale:** if the latest fiscal year ended more than 455 days before
  `as_of` (one year plus the longest 10-K deadline and a margin), the contract
  is `REVIEW_REQUIRED` with `STALE_LATEST_FISCAL_YEAR`.
- **`annual_data_integrity`:**
  - `VERIFIED` when the split status is fine and the core is complete (EPS
    3-year CAGR, the last three YoY statuses known, and ROE meaningful);
  - `VERIFIED_WITH_PARTIAL_CORE_DATA` when the core is incomplete but known;
  - `REVIEW_REQUIRED` for a split or stale problem, or non-meaningful ROE
    (H2).

## 7. Contract `a-input-contract-v1`

Same pattern as C v3. Top-level values:

```text
latest_fiscal_year, latest_annual_eps, annual_eps_yoy_pct (last 3, oldest first),
annual_eps_status (last 3), eps_cagr_3y_pct, eps_cagr_5y_pct, down_years,
loss_years, roe_latest_pct, roe_3y_avg_pct, sales_cagr_3y_pct,
latest_sales_yoy_pct, annual_data_integrity, split_integrity_status
```

The response also carries provenance per input (fiscal years, tags, filings,
basis factors, raw ids, reasons), an `integrity` block, `as_of`,
`company_id`, and the versions (`a-input-contract-v1`, `sec-annual-v1`,
`split-basis-v1`, catalog).

## 8. A Score v1 (proposal, needs human approval)

**Classic O'Neil result**, evaluated in this order:

1. `INSUFFICIENT_HISTORY` when the three latest YoY statuses are not all known;
2. `FAIL_LOSS_YEAR` when any of the last three years is a loss;
3. `FAIL_EPS_GROWTH` when any of the last three YoY values is below 25%;
4. `FAIL_ROE` when the latest ROE is below 17%, or not meaningful;
5. `PASS` otherwise.

**Graded score (100 points, renormalized over available components like C):**

| Component | Max | Curve (input → points) |
|---|---|---|
| EPS 3-year CAGR | 30 | 0→0, 10→10, 15→15, 20→20, 25→25, 35→28, 50→30 |
| EPS consistency (last 3 YoY) | 20 | each year 0→0, 10→3, 20→5.5, 25→6.67 (sum of 3) |
| EPS stability | 10 | 0 down years → 10, 1 → 4, ≥2 → 0; any loss year → 0 |
| ROE (latest) | 25 | 0→0, 10→8, 15→18, 17→21, 20→23, 25→25 |
| Sales 3-year CAGR | 15 | 0→0, 5→5, 10→10, 15→13, 20→15 |

- A loss year or a negative CAGR scores 0 points and counts as available
  (unfavorable, not missing, as in C v1.3).
- A loss-to-profit endpoint makes the CAGR unavailable, with review. In
  consistency, a `LOSS` year scores 0, while a `LOSS_TO_PROFIT` or `NO_DATA`
  year makes the whole component unavailable (its growth cannot be expressed).
- Non-meaningful ROE is unavailable, flagged `ROE_NOT_MEANINGFUL`, and sent to
  review.
- ROE above 100% is flagged `ROE_ABOVE_100_PCT` (diagnostic only; it can come
  from equity shrunk by buybacks, as in AAPL, or from exceptional profitability,
  as in NVDA; the flag does not assert which).
- Classes, status, and usability follow C: `REVIEW_REQUIRED` integrity →
  `REVIEW_REQUIRED_DATA`; fewer than 80 available points → `PARTIAL_SCORE`.

## 9. Delivery plan

1. Catalog v4 and re-ingestion of AAPL, MSFT, and NVDA (adds equity facts).
2. `annual_v3.py` + tests (synthetic and real AAPL/NVDA fixtures, including
   NVDA split years).
3. `a_contract_v1.py` + tests.
4. After weight approval: `a_score_v1.py` + tests; runner `a_v3_runner.py`.
5. Live run on the three companies, documentation, commits for review.

## 10. Acceptance

- NVDA annual EPS is split-consistent: FY2023 YoY is about −55%, not −95.6%.
- An AAPL ROE in the hundreds is computed and flagged `ROE_ABOVE_100_PCT`.
  A company with negative equity gets ROE unavailable and goes to review.
- The 3-year CAGR never uses an older window and never crosses tags.
- The full offline suite passes; C v2/v3 behavior is unchanged.
