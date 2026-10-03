# N — New Highs and SEC Catalysts Design (v1)

**Status:** Approved 2026-10-03, including the N Score v1 weights (§9).
Steps 1–6 of §10 implemented offline, N Score v1 included (`n_score_v1.py`,
`n-1.0-exp`); migration `2026-10-03_price_bars_v1.sql` written but **not
applied** yet.

**Date:** 2026-10-03

**Builds on:** `2026-10-02-fundamentals-v3-design.md` (evidence model, PIT,
`split-basis-v1`, ingestion runs) and the C/A contract and score conventions.

## 1. Human decisions (2026-10-03)

- **H1 — Scope.** N measures the position of the price relative to its highs
  (quantitative, scored) and lists official SEC catalysts as an informative,
  **unscored** signal: management changes (8-K Item 5.02) and completed
  acquisitions or dispositions (8-K Item 2.01). New products have no official
  structured source and are out of scope.
- **H2 — Prices.** Daily Yahoo bars stored literally as append-only raw
  evidence and converted on read to the split basis in force at `as_of` with
  the SEC-verified `split-basis-v1` events, like per-share fundamentals.
- **H3 — Boundary.** N measures highs only: distance to the 52-week high,
  recent new highs, and the multi-year high. Bases, pivots, breakout volume,
  and temporal states belong to the technical layer (pending the reference
  document "Score Fase de Entrada v2").

## 2. Verified provider behavior (2026-10-03)

`yfinance.Ticker(t).history(auto_adjust=False, actions=True)` checked on NVDA
around its 10:1 split of 2024-06-10:

- `Open/High/Low/Close` **and `Volume`** before the split are already divided
  by 10 (2024-06-05 close 122.44, not ~1224). Yahoo has no truly unadjusted
  series: every bar is on the split basis of the **observation date**.
- `Close` is not dividend-adjusted; `Adj Close` is (122.09 vs 122.44).
- Index: one row per session, midnight `America/New_York`; metadata carries
  `exchangeTimezoneName` and `currency`. `period="max"` returns 6,967 sessions
  since 1999-01-22.

**Consequence for H2:** "raw" means the literal response. Each stored bar's
basis is its `observed_at` date, exactly the rule `split-basis-v1` already
applies to Yahoo fundamentals ("Yahoo restates history retroactively").

## 3. Technical decisions (for review)

- **T1 — Prices used.** `Close` (not dividend-adjusted) for position; daily
  `High` for highs, as O'Neil measures new highs intraday. `Adj Close` and
  `Volume` are stored but unused by N (volume is for the technical layer).
- **T2 — Window.** Ingestion requests 6 calendar years, matching the split
  capture window, so every bar used can be adjusted with reconciled events.
  The "multi-year high" is therefore a **5-year** high, not an all-time high
  (older splits are not reconciled; an all-time high is deferred).
- **T3 — PIT.** Bars are visible only when `observed_at <= as_of` and
  `bar_date <= as_of.date()`. As with D1, a historical (backfilled) mode is
  deferred: a past `as_of` only sees bars actually observed by then.
- **T4 — Final bars only.** A bar is stored only if final when observed:
  `bar_date` is before the observation date in the exchange timezone, or the
  observation is at or after 20:00 exchange time on `bar_date`. Intraday
  partial bars are discarded at ingestion (counted in run metadata).
- **T5 — Catalysts from submissions.** The SEC submissions JSON already
  fetched by ingestion has an `items` column (`"5.02,9.01"`). It is parsed for
  `8-K`/`8-K/A` and stored literally; no extra request is needed.

## 4. Storage (migration `2026-10-03_price_bars_v1.sql`)

`yahoo_price_bars_raw` — append-only:

| Column | Notes |
|---|---|
| `company_id`, `ticker` | |
| `bar_date DATE` | session date in exchange timezone |
| `open, high, low, close, adj_close NUMERIC(38,10)` | literal |
| `volume NUMERIC(38,0)` | literal |
| `currency TEXT`, `exchange_timezone TEXT` | from metadata |
| `observed_at TIMESTAMPTZ`, `run_id` → `ingestion_runs` | |

- A bar is inserted only when it differs from the latest stored observation
  of the same `(company_id, bar_date)` (unchanged bars are counted, not
  re-inserted). Unique `(company_id, bar_date, observed_at)`.
- Checks: `low <= open, close <= high`, `low > 0`, `volume >= 0`.

`sec_filing_items` — append-only: `company_id, accession, item TEXT,
observed_at, run_id`; unique `(company_id, accession, item, observed_at)`.
Joined to `sec_filings` by accession for form, filing date, and acceptance.

Migrations are applied only with explicit authorization (D4 covers it, but it
will be stated before applying).

## 5. Price series `price-series-v1` (pure, on read)

1. Load bars with `observed_at <= as_of`; for each `bar_date`, keep the latest
   observation.
2. Convert each bar to the `as_of` basis: price ÷ `basis_factor(events,
   observed_at.date(), as_of)`, events from the same reconciliation C v3 uses
   (`load_split_reconciliation`). Bars whose observation date is within
   `UNCERTAIN_BASIS_DAYS` of an event are flagged `PRICE_BASIS_UNCERTAIN`.
3. **Self-check:** where two observations of the same bar exist across a split
   event, their ratio must match the event ratio within
   `SUSPECTED_RATIO_TOLERANCE`; otherwise `PRICE_BASIS_CONFLICT` →
   `REVIEW_REQUIRED`. An unexplained session-to-session jump whose ratio
   matches a common split ratio (`suspected_split_ratio`) on bars of one
   observation → `PRICE_SUSPECTED_UNRECORDED_SPLIT` → `REVIEW_REQUIRED`.
4. Split integrity `REVIEW_REQUIRED` (as in C/A) propagates to N.
5. Currency must be `USD` (foreign filers are already excluded); otherwise
   `PRICE_CURRENCY_UNSUPPORTED`.

## 6. Calculations `n-highs-v1`

Let `last` = the latest bar with `bar_date <= as_of`, and 52 weeks = the 252
sessions ending at `last` (sessions, not calendar days).

- `close_last`, `high_52w` = max daily `High` over 252 sessions,
  `pct_below_high_52w = 1 − close_last / high_52w` (≥ 0).
- `new_high_recent`: a session within the last 20 whose `High` equals
  `high_52w`.
- `sessions_since_high_52w`.
- `high_5y` = max `High` over the window (≥ 1,000 sessions required),
  `pct_below_high_5y`.
- **Statuses:** fewer than 252 sessions → `INSUFFICIENT_HISTORY` (recent IPOs
  stay explicit, not unfavorable); `last` older than 5 sessions before `as_of`
  → `STALE_PRICES`; a gap of more than 5 business days inside the 252-session
  window → `PRICE_GAP` (review). Fewer than 1,000 sessions only makes the 5-year
  fields unavailable.

## 7. Catalysts `sec-catalysts-v1` (informative, unscored)

- 8-K/8-K-A with Item 5.02 or 2.01, `acceptance_at <= as_of` (filing date at
  00:00 UTC when acceptance is missing, flagged) and `observed_at <= as_of`,
  within the 365 days before `as_of`.
- Output per event: item, form, filing date, accession, EDGAR URL. Counts per
  item. No interpretation: a 5.02 can be a departure or an appointment, and is
  reported as such.
- Never changes the score, status, or usability of N.

## 8. Contract `n-input-contract-v1`

Fields: `as_of`, `last_bar_date`, `close_last`, `high_52w`,
`pct_below_high_52w`, `new_high_recent`, `sessions_since_high_52w`,
`high_5y`, `pct_below_high_5y`, `sessions_available`, `split_status`,
`integrity_status`, `reasons`, `catalysts`, versions (`price-series-v1`,
`n-highs-v1`, `split-basis-v1`, `sec-catalysts-v1`). Null fields carry explicit
reasons, as in C/A.

## 9. N Score v1 (approved 2026-10-03)

**Classic O'Neil result**, in order:

1. `INSUFFICIENT_HISTORY` / `STALE_PRICES` when §6 says so;
2. `FAIL_FAR_FROM_HIGH` when `pct_below_high_52w > 15%`;
3. `PASS` otherwise.

**Graded score (100 points, renormalized over available components):**

| Component | Max | Curve (input → points) |
|---|---|---|
| Distance to 52-week high | 60 | 0%→60, 5%→55, 10%→45, 15%→30, 25%→10, 35%→0 |
| Recent new 52-week high | 25 | high in last 20 sessions → 25; 21–60 → 12; older → 0 |
| Distance to 5-year high | 15 | 0%→15, 10%→10, 25%→4, 40%→0 |

- Classes, status, and usability follow C/A: `REVIEW_REQUIRED` integrity →
  `REVIEW_REQUIRED_DATA`; fewer than 80 available points → `PARTIAL_SCORE`.
- The score measures fulfilment of the criterion, not the probability of a
  rise. Being at a high is not an entry signal (that is the technical layer).

## 10. Delivery plan (TDD, one commit per step after review)

1. Spec approved → migration `price_bars_v1` (+ `sec_filing_items`).
2. `prices_v3.py` (pure series, basis, self-checks) with tests on recorded
   NVDA bars around the 2024 split (fixture).
3. `n_highs_v1` calculations and statuses.
4. Catalysts: `items` parsing in `parse_submission_arrays`, storage, selection.
5. `n_contract_v1.py`, `n_score_v1.py`.
6. Ingestion operations `yfinance.daily_bars` and submission items in
   `ingest_v3`; `n_v3_runner.py` (`python -m database.n_v3_runner NVDA`);
   `batch_v3` gains N.
7. Live run on the 30 ingested companies; audit note.

## 11. Acceptance

- Offline tests green, `git diff --check` clean.
- NVDA fixture: bars observed after the split need no factor; a pre-split
  observation of 2024-06-05 (1,224.40) converts to 122.44 at a post-split
  `as_of`; the conflict check catches a wrong ratio.
- A past `as_of` never sees later observations or later bars.
- A partial intraday bar is never stored.
- Catalysts never change score or status.

## 12. Deferred

- All-time high beyond the reconciled split window.
- Historical (backfilled) PIT mode for prices.
- New products (no official structured source).
- Relative strength vs market (belongs to L).

## 13. Implementation notes (2026-10-03)

- **Micro-unit prices.** Yahoo serves float32 prices (95.20 arrives as
  95.19999694824219); bars are stored rounded to 6 decimals so every quoted
  digit is kept and repeated observations compare equal (§4 "only changes").
- **Rejected rows** (NaN, non-positive price, OHLC inconsistent, negative
  volume) are not stored; their dates and reasons go to the run metadata.
  A rejected row inside the 52-week window shows up as a gap only if it
  creates one longer than 5 business days.
- **Basis date.** A bar's basis is its observation date **in the exchange
  timezone** (an observation at 01:00 UTC on an ex-date is still the previous
  day in New York, before Yahoo adjusts).
- **Unrecorded split check** needs the jump at both the open and the close
  (same common ratio), so a real −50% close after a normal open is not
  mistaken for a split.
- **Split verdict reused from A.** N applies the provider events the annual
  view did not reject and takes its split status from the same
  `effective_split_status` call as A.
- **Missing acceptance (§7 corrected).** The fallback is the end of the EDGAR
  filing day (00:00 New York of the next day), not 00:00 UTC of the filing
  day, which would leak a filing into an earlier `as_of`.
- **8-K items.** Stored once per `(company_id, accession, item)`; a foreign
  key to `sec_filings` guarantees the filing exists. The check only rejects
  empty or padded codes, because pre-2004 8-Ks use single-digit items. Items
  of filings already stored are observed on the next ingestion, so a past
  `as_of` does not see them (consistent with T3). Submissions runs carry the
  new contract `sec-submissions-v2`.
- **Five-year window** is calendar-based (bars after the same day five years
  before the last bar) and requires the first bar within 10 days of its start.
- **Exchange timezone** of stored bars is not yet read back; the series
  assumes `America/New_York`, true for every supported (domestic) company.
