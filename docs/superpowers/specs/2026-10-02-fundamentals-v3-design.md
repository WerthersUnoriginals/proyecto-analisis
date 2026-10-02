# Fundamentals v3 Design

**Status:** Implemented 2026-10-02 (uncommitted, pending review). Decisions
D1–D4 approved by the human; technical decisions T1–T9 taken by the
implementer and listed for review. §5 was amended during implementation (see
§12).

**Date:** 2026-10-02

**Base commit:** `6817390`

**Motivation:** `docs/audits/2026-10-02-c-block-audit.md`. The v2 pipeline
selects one value per period from the latest filing, never derives Q4, mixes
split bases, resolves identity with later knowledge, and has no repeatable
ingestion. These defects produce wrong but plausible C inputs (NVDA: 4 of 17
EPS YoY values wrong), and they would make A wrong as well.

## 1. Scope

In scope:

- verbatim, append-only raw evidence for SEC Company Facts and EDGAR filing
  metadata, with ingestion-attempt records for every provider;
- point-in-time normalization performed on read from raw evidence;
- a verified split-basis adjustment for per-share and share-count metrics;
- Q4 derivation for revenue and net income;
- a new quarterly selection policy and a new C input contract with the same
  eleven keys, consumed by the unchanged `c_score_v1`;
- a repeatable, ticker-agnostic ingestion command.

Out of scope: changing `fundamental_c.py`, `c_score_v1.py`, or any v2 module,
view, or migration. v2 remains as the frozen legacy-compatible path and its
tests and baselines (`75/60/11`) remain valid for v2 only.

## 2. Human decisions

- **D1 — Point-in-time.** Do not invent data. Selection uses
  `observed_at <= as_of` (what this installation knew). The EDGAR
  `acceptanceDateTime` of each filing, a real public timestamp, is stored as
  `source_available_at`. Nothing is inferred from `filed_date`. A future
  "public availability" mode for backtests is a separate decision.
- **D2 — Splits.** Per-share values (EPS) and share counts are converted to
  one basis by the system, using verified split events. The conversion is
  checked against SEC's own original/restated pairs, and any contradiction
  fails closed.
- **D3 — Q4.** Derive Q4 revenue and net income as FY minus 9M YTD from SEC.
  Q4 EPS is not derived; it comes from Yahoo when available.
- **D4 — Authorization.** General authorization for SEC/Yahoo reads,
  migrations, and writes to `canslim`, reported after each step. Commits still
  require review, and pushes require explicit authorization.

## 3. Technical decisions (for review)

- **T1 — Normalize on read.** v3 does not persist normalized rows. It
  normalizes the raw evidence visible at `as_of` with pure, versioned
  functions. This removes the lookahead found in v2 (identity computed with
  later evidence) and keeps raw as the only stored truth. Reproducibility comes
  from immutable raw evidence plus version constants.
- **T2 — Verbatim SEC raw table.** `sec_companyfacts_raw` stores Company Facts
  items exactly as published (tag, unit, start, end, val, accn, fy, fp, form,
  filed, frame) for every tag in the versioned catalog. It covers all
  durations, so the annual and YTD facts that A and Q4 derivation need are
  retained. Metric mapping is normalization, not raw.
- **T3 — Concept-consistent growth.** Every YoY pair uses one source and one
  XBRL tag. If the newest period's preferred tag is absent a year earlier, the
  next catalog tag present in both periods is used. Tags are never mixed inside
  one ratio.
- **T4 — Latest-quarter rule.** `latest_*_yoy_pct` always describes the latest
  effective quarter of that metric. If that YoY cannot be computed, the value is
  `None` with reason `LATEST_QUARTER_YOY_UNAVAILABLE`; an older quarter is never
  substituted.
- **T5 — Acceleration across sources.** The latest and previous YoY must be
  consecutive quarters (70–120 days apart). Each YoY is single-source, but the
  two may come from different sources; provenance then carries
  `MIXED_SOURCE_ACCELERATION`.
- **T6 — Stale data.** If the latest effective quarter ended more than 200 days
  before `as_of`, the result is `REVIEW_REQUIRED` with `STALE_LATEST_QUARTER`.
- **T7 — `EarningsPerShareBasicAndDiluted`** is a diluted EPS candidate (by
  definition basic equals diluted). It ranks below `EarningsPerShareDiluted`
  and keeps its tag in provenance.
- **T8 — Yahoo snapshots.** Each Yahoo fetch records which raw rows it
  returned (`ingestion_run_items`). The visible Yahoo value of a period is the
  one in the latest successful run at `as_of`. A revision no longer blocks a
  period forever, and re-observing an older value is represented correctly.
- **T9 — Split sources.** yfinance split events and SEC
  `StockholdersEquityNoteStockSplitConversionRatio1` facts are reconciled.
  Agreement verifies an event. A split reported by only one source, or a
  restated SEC pair whose ratio no event explains, fails closed.

## 4. Storage (migration `2026-10-02_evidence_v3.sql`)

- `ingestion_runs(id, company_id, provider, operation, started_at,
  completed_at, status, error_code, item_count, contract_version)`. Status is
  `SUCCESS` or `FAILED`. Every provider attempt is recorded, including
  failures.
- `ingestion_run_items(run_id, raw_table, raw_id)`: the evidence returned by a
  run (used for Yahoo snapshots).
- `sec_companyfacts_raw(id, company_id, cik, taxonomy, tag, unit,
  period_start, period_end, value, accession, fiscal_year, fiscal_period, form,
  filed_date, frame, observed_at, run_id)`. The unique identity is
  `(company_id, taxonomy, tag, unit, period_start, period_end, accession)` with
  `NULLS NOT DISTINCT`. A retry with a different value raises an error; it never
  overwrites.
- `sec_filings(id, company_id, accession UNIQUE, form, filing_date,
  acceptance_at, report_date, observed_at, run_id)`.
- `companies.cik` is filled from SEC when it is missing (existing column).

Yahoo raw stays in `fundamentals_raw` through the existing importer, now
wrapped in an ingestion run with run items. yfinance split captures stay in
`corporate_action_captures/events`.

## 5. SEC normalization `sec-quarterly-v3`

Input: `sec_companyfacts_raw` rows with `observed_at <= as_of`.

Tag catalog `sec-tag-catalog-v3` (ordered by preference):

```text
EPS_DILUTED:    EarningsPerShareDiluted, EarningsPerShareBasicAndDiluted
REVENUE:        RevenueFromContractWithCustomerExcludingAssessedTax, Revenues,
                SalesRevenueNet, SalesRevenueGoodsNet
NET_INCOME:     NetIncomeLoss, ProfitLoss
DILUTED_SHARES: WeightedAverageNumberOfDilutedSharesOutstanding,
                WeightedAverageNumberOfShareOutstandingBasicAndDiluted
SPLIT_RATIO:    StockholdersEquityNoteStockSplitConversionRatio1
```

Duration classes (days, inclusive): `QUARTER` 70–110, `YTD_6M` 160–200,
`YTD_9M` 250–290, `ANNUAL` 330–400; anything else is `OTHER` and is ignored by
C. Accepted forms: `10-Q`, `10-Q/A`, `10-K`, `10-K/A`.

Fiscal identity of a quarterly period (amended, see §12):

1. Fiscal years come from the distinct end dates of 350–380-day intervals in
   10-K filings. Missing years in a gap are projected; a gap shorter than 350
   days is a fiscal-year change, and quarters inside it are `UNRESOLVED`.
2. A quarter's key is its position inside the fiscal year that contains its
   end date: `round(days since year start / 91.3125)`, in 1–4.
3. A filing's claim about its *own* period (`fp` of the latest-ending
   quarterly fact of a 10-Q) must agree on the quarter number, or the quarter
   is `UNRESOLVED`. Comparative facts never carry identity.
4. With no 10-K yet, own-period claims identify quarters, and other quarters
   are projected by whole years from them.
5. Two intervals with the same key are `UNRESOLVED`.

`UNRESOLVED` periods are ineligible and reported.

## 6. Split basis `split-basis-v1`

Events: verified splits in the window, after T9 reconciliation. An event's
date is the yfinance ex-date. A SEC ratio fact corroborates an event when the
ratios are equal and the dates fall within 45 days of each other.

Basis of an observation:

- SEC fact: the issuer's basis at its `filed_date`. SEC rules require
  retroactive restatement when a split occurs before the statements are issued.
- Yahoo value: the basis at the date of its observing run (Yahoo restates
  history retroactively).

The factor to the `as_of` basis is the product of the ratios of verified
events with `basis_date < event_date <= as_of`. EPS is divided by the factor;
share counts are multiplied by it. If a filing date lies within 7 days of an
event date, its basis is uncertain. It is resolved only by a corroborating
pair (below); otherwise that fact is ineligible with
`SPLIT_BASIS_UNCERTAIN`.

Verification: for every period with facts from two or more filings, all
values converted to the `as_of` basis must agree within
`max(0.0051 per-share, 0.5%)` for EPS and 0.5% for shares. If a disagreement
equals a common split ratio (2, 3, 4, 5, 8, 10, 15, 20, 1.5, or their inverses)
within 2% and no verified event explains it, the result is
`UNDECLARED_SPLIT_SUSPECTED`. That makes the metric's split status
`REVIEW_REQUIRED`. Other disagreements are restatements: the latest filing wins
and the difference is recorded.

## 7. Q4 derivation `q4-derivation-v1`

For REVENUE and NET_INCOME, and for one tag:

```text
Q4 = ANNUAL(start=S, end=E) - YTD_9M(start=S, end=E9)
```

Both facts must be visible at `as_of` and share the tag, unit, and exact start
`S`. `E9` must fall 70–110 days before `E`. Each side uses its latest visible
filing. The derived observation has period `(E9 + 1 day, E)`, quarter 4, the
fiscal year of the annual fact, `observation_kind=DERIVED`, its availability
equal to the later of the two inputs, and both raw IDs as lineage. A reported
Q4 for the same tag always wins over a derived one; when both exist they are
compared, and a difference above 0.5% is recorded as `Q4_DERIVATION_MISMATCH`.

## 8. Quarterly selection `c-quarterly-v3`

Per metric and fiscal quarter (canonical period end = SEC end date):

1. SEC reported (split-adjusted), else SEC derived Q4 (revenue/NI), else Yahoo
   aligned to the quarter (35-day window and fiscal identity, as in v2), else
   no value.
2. Yahoo identity uses only SEC evidence visible at the Yahoo observation's
   own `observed_at` (no lookahead).
3. The SEC–Yahoo comparison is recorded per quarter on adjusted values, using
   the v2 bands.

Growth: YoY for a quarter uses its comparable quarter (same fiscal quarter,
fiscal year − 1). Both observations come from the same source and the same
tag (T3), and the prior value must be strictly positive. Loss-to-profit
applies when the prior value is ≤ 0 and the current value is > 0.

## 9. C input contract `c-input-contract-v3-11`

The eleven keys are unchanged, so `c_score_v1` consumes them as is. The
semantics follow T3–T6, and the response carries per-input provenance and
reasons. `data_integrity` keeps the v2 domain and aggregation rules but takes
inputs from v3:

- data quality from the six core scalars;
- split status: `NO_RECENT_SPLITS` (no events and no SEC ratio facts in six
  years, with a successful capture), `VERIFIED_ALREADY_ADJUSTED` (events
  verified and basis consistent), `REVIEW_REQUIRED` (contradiction, undeclared
  split, uncertain basis in a used value), `UNKNOWN` (no successful capture
  visible);
- SEC/Yahoo consistency over the quarters of the last three years on adjusted
  values;
- `STALE_LATEST_QUARTER` forces `REVIEW_REQUIRED`.

## 10. Ingestion `python -m database.ingest_v3 TICKER`

The command is idempotent and runs these steps, each recorded as an ingestion
run:

1. resolve the CIK (SEC catalog);
2. Company Facts → `sec_companyfacts_raw`;
3. submissions (recent plus paged files) → `sec_filings`;
4. Yahoo quarterly evidence → `fundamentals_raw` plus run items;
5. yfinance splits → corporate-action capture.

A failure in one step is recorded and does not erase the others. C then
reports the missing evidence (fail-closed).

## 11. Acceptance

- Offline tests for every rule above, including real-data fixtures trimmed
  from NVDA and AAPL Company Facts.
- NVDA EPS YoY at the four known split-affected quarters matches the
  split-consistent values, which are close to the correct values in the audit.
- AAPL Q4 revenue/NI derived values equal FY − 9M exactly.
- AAPL, MSFT, and NVDA ingested; the C v3 contract and score computed for all
  three, with a frozen v3 baseline fixture.
- The full offline suite passes; v2 tests are unchanged.

## 12. Implementation notes from real data (2026-10-02)

These rules were changed or confirmed while running against real SEC/Yahoo
evidence for AAPL, MSFT, and NVDA:

- **`fy` is not reliable.** NVDA labels its fiscal-2012 quarters `fy=2011` but
  labels fiscal 2022 correctly. Fiscal years therefore come from real 10-K
  year-end dates, and quarters from their position inside the fiscal year (§5
  amended). An own-period `fp` claim only corroborates; a contradiction makes
  the quarter `UNRESOLVED`. Labels use one offset taken from the latest 10-K
  (NVDA FY ending 2026-01-25 → 2026, as NVDA names it).
- **Analysis window.** Values cover six years plus one year of comparables, and
  split checks need both filings inside the window. Without it, AAPL's 2010
  revenue-recognition restatement (FY2009 EPS 1.35 → 2.01) looks like a 3:2
  split.
- **One split, several SEC dates.** NVDA tags its 2021 split on 2021-06-03
  (approval) and 2021-07-19 (effective). SEC ratio facts with the same ratio
  within 120 days of a known event describe that event.
- **EDGAR `acceptanceDateTime` is UTC.** Verified on 1,000 NVDA filings. The
  value is stored as published.
- **Raw is verbatim.** Real filings violate any fixed acceptance/filing-date
  bound (weekend acceptances; a 2003 "NO ACT" accepted after its filing date),
  so no such constraint exists on raw tables.
- **SEC access.** `www.sec.gov` (ticker list) requires a User-Agent with a
  contact address; `data.sec.gov` does not. The CIK comes from `companies` or
  `TICKER:CIK` and is verified against SEC submissions. Setting
  `CANSLIM_SEC_USER_AGENT` enables automatic lookup.

Live result on 2026-10-02: AAPL, MSFT, and NVDA give the same eleven inputs
and score as the legacy live run (62.69, 65.77, 87.02). The defects in the
audit do not affect the current quarters of these three companies, but they
do affect history, Q4, and other issuers, and v3 corrects them (offline
tests).

## 13. Consequences for A

The A draft must be revised to build on v3 raw evidence and normalization
(annual facts are already stored) and on `split-basis-v1`. Its latest-filing
annual rule and its exclusion of split handling must change. The heavy rollout
protocol in A §6 (capability gates, writer inventories) is disproportionate
for a single-writer local installation and should be simplified in the A
review.
