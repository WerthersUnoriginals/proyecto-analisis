# C Block Audit

**Date:** 2026-10-02

**Base commit:** `6817390` (C contract closed at `4bb7a81`)

**Scope:** everything implemented for C — Current Earnings before the A spec
review: SEC/Yahoo import, normalization v1/v2, effective selection (Python and
`fundamentals_effective_current`), the C adapter and 11-input contract, data
and split integrity, corporate-action capture, live runner, legacy oracle.

**Method:** code reading, the offline suite (408 tests, OK, 4 skipped), an
offline reproduction script, and one read-only SEC Company Facts download for
NVDA and AAPL (authorized 2026-10-02). No code, database, or migration was
changed.

## Summary

The implementation is technically careful: append-only raw/normalized storage,
`observed_at` filtering before resolution, `Decimal` arithmetic, explicit
versions, fail-closed handling of ambiguity, and a SQL view equivalent to the
Python selector. The defects found are not coding slips; they are assumptions
about how real provider data behaves that the AAPL fixture never exercises.

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Latest EPS and latest EPS YoY can refer to different quarters | High | Verified (offline reproduction) |
| 2 | Q4 is never derived; SEC rarely publishes standalone Q4 | High | Verified (SEC data, fixture) |
| 3 | Latest-filing selection mixes split-adjusted and unadjusted SEC values | High | Verified (NVDA SEC data) |
| 4 | Point-in-time means "known to this database", not "publicly available" | High (design) | Verified (code) |
| 5 | No repeatable ingestion pipeline beyond the frozen AAPL backfill | Medium | Verified (code) |
| 6 | yfinance split failures are recorded as complete captures with no splits | Medium | Verified (code + provider behavior) |
| 7 | One observed Yahoo revision blocks that period permanently | Medium | Verified (code) |
| 8 | SEC tag chosen by period count, and only the winning tag reaches raw | Medium | Verified (code) |
| 9 | Any quarterly fact first seen in a 10-K is labeled Q4 | Low–Medium | Verified (code) |
| 10 | Minor items and repository hygiene | Low | Verified |

## 1. Latest EPS and latest EPS YoY from different quarters

**Where:** `database/effective_fundamentals.py` (`select_effective_observations`,
`annual_comparisons_by_source`), `database/c_fundamentals_adapter.py`
(`_latest_acceleration`, `_latest_observation`).

The effective set keeps a Yahoo observation only when no SEC observation lies
within 35 days. When the newest quarter exists only in Yahoo, the Yahoo
comparable from one year earlier has an SEC counterpart and was therefore
dropped, so Yahoo has no YoY for the newest quarter. The adapter then picks
the SEC acceleration whose latest YoY belongs to the previous quarter, while
`latest_eps` comes from the newest Yahoo quarter. Nothing flags the mismatch.

Offline reproduction (SEC quarters through 2025-06-28, Yahoo-only 2025-09-27):

```text
latest_eps           2.0    (2025-09-27, Yahoo)
latest_eps_yoy_pct   20.0   (2025-06-28, SEC)
previous_eps_yoy_pct 10.0
eps_acceleration_pp  10.0
```

The legacy oracle computed Yahoo growth on the full Yahoo series and would
have produced a YoY for 2025-09. The dual-run did not detect the divergence
because the AAPL baseline does not contain this case.

Consequences:

- C scores a stale quarter without saying so.
- `c_live_runner` derives `previous_eps_period` from the latest EPS observation's
  annual pair. In this case it is `None`, `current_yoy_crosses_split()` returns
  `None`, and `classify_data_quality()` treats `None` as "does not cross". The
  split-crossing check therefore fails open (`database/c_data_integrity.py:343`,
  `database/c_live_runner.py:139`).
- `small_base` in `c_score_v1` infers the previous EPS from `latest_eps` and
  `latest_eps_yoy_pct`, which here describe different quarters.

## 2. Q4 is never derived

SEC Company Facts contains standalone Q4 (3-month) facts only when a 10-K
includes quarterly data. Since the SEC removed the Item 302(a) quarterly-data
requirement (effective 2021), most issuers stopped doing so:

- AAPL: standalone Q4 EPS exists through FY2020 only (the fixture's Q4 table
  confirms this); FY2021 onwards have no SEC Q4.
- NVDA: no standalone Q4 EPS at all in the downloaded window.

The C pipeline never derives Q4 = FY − 9M YTD (the schema already admits
`DERIVED` observations). In addition, the SEC importer keeps only
quarterly-duration facts, so the annual facts needed for derivation are not
even stored.

Consequences:

- About one quarter in four, the latest YoY depends on Yahoo. Yahoo usually
  returns only about 5 quarters, so a Yahoo Q4 rarely has a Yahoo comparable.
  The result is a stale quarter or a missing acceleration.
- The "last 4 YoY" used by trend quality and persistence spans five calendar
  quarters. The AAPL fixture shows this: the series jumps from 2025-06-28 to
  2025-12-27.

## 3. Split-adjusted and unadjusted SEC values are mixed

**Where:** latest-filing rule in `_resolve_source_periods`, the view's
`source_rank`, `resolve_legacy_sec_metric`, and the legacy
`_series_from_sec_concept`.

A quarter is reported at most twice: in its original 10-Q, and as the
prior-year comparative in the next year's 10-Q. Comparatives are restated
after a split, and older periods are never re-reported. Selecting "latest
filing per period" therefore yields a series where some periods are adjusted
and others are not.

NVDA evidence (SEC Company Facts, `EarningsPerShareDiluted`, 3-month; splits
4:1 on 2021-07-20 and 10:1 on 2024-06-10):

| Period end | Original filing | Latest filing | Comparable latest | YoY with current rule | Correct YoY |
|---|---|---|---|---|---|
| 2021-05-02 | 3.03 | 0.76 (adjusted) | 1.47 (not adjusted for 4:1) | −48.3% | +106% |
| 2023-07-30 | 2.48 | 0.25 (adjusted) | 0.26 (not adjusted for 10:1) | −3.8% | +854% |
| 2023-10-29 | 3.71 | 0.37 (adjusted) | 0.27 (not adjusted) | +37.0% | +1274% |
| 2024-04-28 | 5.98 | 0.60 (adjusted) | 0.82 (not adjusted) | −26.8% | +629% |

Four of NVDA's 17 computable YoY values in the window are wrong. Split
integrity does not catch this: the implied-EPS check uses the same restated
EPS, NI, and shares, so it reports `ALREADY_ADJUSTED`. `current_yoy_crosses_split`
only checks the latest YoY window.

For C today, the wrong values fall outside the last four for NVDA. A split
inside the last 12–15 months, combined with missing Q4s (finding 2), puts one
of them into the trend and persistence window. For an older split, the
`eps_yoy_pct` history is wrong but not scored.

Annual facts behave the same way. A 10-K restates three fiscal years, and
older years keep their original basis. NVDA annual diluted EPS by latest
filing: FY2021 1.73 (4:1-adjusted only), FY2022 3.85 (not adjusted for 10:1),
FY2023 0.17, FY2024 1.19, FY2025 2.94, FY2026 4.90. FY2023 YoY would be −95.6%
instead of about −55%. See "Impact on A" below.

## 4. Point-in-time means "known to this database"

`observed_at` is the download time (`fetched_at`), and `source_available_at`
is always NULL. Consequences:

- Any `as_of` earlier than the first download returns no evidence, so no
  historical backtest is possible with the current data. This affects the
  future Experience Store.
- Yahoo fiscal identity and eligibility are computed against whatever SEC
  calendar exists when normalization runs, not as of the Yahoo observation.
  A Yahoo row normalized after a later SEC import therefore carries later
  knowledge into earlier `as_of` queries.

The draft A spec (§11) already takes the strict position (no availability
inferred from `filed_date`; an annual backfill cannot certify history). This
is coherent, but it is a product decision to make explicitly: it rules out
backtesting until history has been observed live.

## 5. No repeatable ingestion pipeline

`database/backfill_semantics_v2.py` executes the v2 migration inside its own
transaction. It also gates on hard-coded AAPL counts (`raw_sec=135`,
`quarterly=19`, `yahoo_eligible=25`, …). It cannot run for another ticker or
for new data. Yahoo v2 identity needs a v1 SEC calendar, and only
`backfill_normalized.py` (SEC v1) produces one. No orchestrator takes
import → normalize v1/v2 → splits capture → effective view for an arbitrary
ticker. "C closed" is therefore closed for the frozen AAPL dataset.

## 6. Split acquisition fails open

`yfinance.Ticker(...).splits` commonly returns an empty series on network or
provider problems instead of raising. `corporate_action_acquisition` then
records `SUCCESS/COMPLETE` with zero events, which yields `NO_RECENT_SPLITS`
and `VERIFIED`. Only `ImportError`, `OSError`, and rate-limit errors count as
failures. The legacy path has the same weakness.

## 7. A single Yahoo revision blocks a period permanently

The Yahoo `source_record_id` is a hash of semantic content without the
observation time. Re-observing a previously seen value reuses the old raw row
and its old `observed_at`. Once a period has two different values for the same
variant, `_resolve_source_periods` (and the view's `group_size > 1`) marks it
ambiguous forever, at every later `as_of`. It also blocks the yfinance
fallback. This is fail-closed but irreversible, and Yahoo frequently revises
the newest quarter.

## 8. Tag selection by coverage, and only the winner is stored

`sec_import.extract_sec_raw_facts` keeps, per metric, the tag with the most
distinct periods, and it persists only that tag's facts. Two consequences:

- A stale tag with longer history can beat the tag the company currently uses.
- Raw is not complete evidence. Discarded tags cannot be re-evaluated without
  a new download. The A spec (§8.5.4) requires retaining all candidates.

## 9. Q4 labeling heuristic

`_original_sec_fiscal_identity` labels any quarterly-duration fact whose
earliest filing is a 10-K/10-K/A with `fp=FY` as Q4. It does not check that
the period ends at fiscal year-end. Quarterly tables in a first 10-K (recent
IPOs, or pre-2021 Item 302 data) can mislabel Q1–Q3.

## 10. Minor items

- `c_score_v1`: when losses widen (prior EPS ≤ 0), EPS growth becomes
  "unavailable" rather than unfavorable. This is mitigated by `PARTIAL_SCORE`
  and review usability. The file is frozen; record it for a future score
  version.
- `yahoo_import` discards the time-series fetch error; no attempt record
  exists, so "no data" and "fetch failed" are indistinguishable.
- `__pycache__/watchlist_screener.cpython-311.pyc` and
  `resultado_watchlist.csv` are tracked despite `.gitignore`.
- The SEC EPS catalog contains only `EarningsPerShareDiluted`. Issuers that
  report only `EarningsPerShareBasicAndDiluted` get no SEC EPS (coverage
  limitation).

## Impact on A (draft spec 2026-09-28)

- **Splits (3):** the A spec's latest-filing rule (§8.6) and its explicit
  exclusion of corporate actions and split integrity (§2.2) would produce
  wrong annual YoY, wrong positive/negative growth counts, and wrong 5-year
  CAGR for any issuer with a split in the window. A 3-year CAGR is wrong
  whenever the split happened after the 10-K that last restated FY N−3. A needs
  a split-basis policy before A1.2.
- **Q4 (2):** deriving Q4 needs the annual SEC facts that A1.2 is the first
  to acquire. The A spec lists Q4 derivation as out of scope. If C is to fix
  Q4, the dependency runs from C to A's annual ingestion, and the order of work
  must reflect it.
- **PIT (4):** A §11 already chooses strict DB-knowledge semantics. Confirm or
  amend it deliberately, together with the C decision.
- **Ingestion (5) and tags (8):** A1.2 needs a repeatable, ticker-agnostic
  ingestion that keeps all candidate tags. Building it once for both blocks
  avoids two divergent pipelines.
- **Findings 1, 6, 7** are C-only, but the fail-open patterns (6) would
  propagate to any A split check that reuses corporate-action captures.

Fixing 1–3 changes C selection or calculation semantics. It requires new
versions; `c-v2.6-compatible-v1` must not be reused. The AAPL baseline
(`sec_effective=75`, `hybrid_effective=60`) would likely survive the fix for
finding 1 but not a Q4 derivation.

## Evidence reproduction

- Offline: build effective rows with `test_c_fundamentals_adapter.effective_row`
  for SEC quarters through 2025-06-28 plus one Yahoo-only 2025-09-27 quarter,
  then call `build_c_fundamental_report`.
- SEC: Company Facts for CIK 0001045810 (NVDA) and 0000320193 (AAPL),
  `us-gaap/EarningsPerShareDiluted`, unit `USD/shares`, filtered to forms
  10-Q/10-K(/A) with 70–110-day (quarterly) or 350–380-day (annual) durations.
  Downloaded 2026-10-02; not persisted.

## Resolution status (2026-10-02, uncommitted)

Implemented as fundamentals v3
(`docs/superpowers/specs/2026-10-02-fundamentals-v3-design.md`):

| # | Status |
|---|---|
| 1 | Fixed: latest inputs always describe the latest quarter; otherwise `None` with reason. |
| 2 | Fixed for revenue and net income (FY − 9M); Q4 EPS from Yahoo by human decision D3. |
| 3 | Fixed: verified split basis (`split-basis-v1`); NVDA cases covered by real-data tests. |
| 4 | Lookahead removed (normalization on read). EDGAR acceptance times stored; historical public-availability mode deferred (D1). |
| 5 | Fixed: `python -m database.ingest_v3 TICKER`, one immutable run per provider attempt. |
| 6 | Mitigated: provider splits reconciled with SEC split-ratio facts and restated pairs. |
| 7 | Fixed: Yahoo snapshot = latest successful run per dataset. |
| 8 | Fixed: all catalog tags stored verbatim; YoY uses one tag for both periods. |
| 9 | Fixed: identity from real fiscal year-end dates and position; `fy` not trusted. |
| 10 | Fixed: C Score v1.3 treats losses as unfavorable (`c_score_v13.py`); generated files untracked. Open: SEC contact User-Agent (human setup). |
