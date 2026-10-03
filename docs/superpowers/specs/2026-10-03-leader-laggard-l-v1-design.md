# L — Leader or Laggard Design (v1)

**Status:** Approved by the human on 2026-10-03, including the L Score v1
weights and classic threshold (§8). Implemented; live run in
`docs/audits/2026-10-03-l-live-26-companies.md`.

**Date:** 2026-10-03

**Builds on:** the N price series (`price-series-v1`, literal Yahoo bars
converted with `split-basis-v1`), EDGAR submissions, and the evidence model
of fundamentals v3.

## 1. Human decisions (2026-10-03)

- **H1 — Universe.** The RS Rating is a percentile against the **S&P 500**,
  taken from the official daily holdings file of the SPDR S&P 500 ETF (SPY,
  State Street).
- **H2 — Formula.** IBD-style weighting, latest quarter double:
  `0.4·C/C₆₃ + 0.2·C/C₁₂₆ + 0.2·C/C₁₈₉ + 0.2·C/C₂₅₂` (sessions back).
- **H3 — Group.** Industry group from the official SEC SIC code in the EDGAR
  submissions; scored.
- **H4 — RS line.** Price ÷ S&P 500 (SPY bars); scored.

## 2. Verified sources (2026-10-03)

- SSGA `holdings-daily-us-en-spy.xlsx`: an xlsx (zip of XML) read with the
  standard library (no new dependency). Header row `Name, Ticker, Identifier,
  SEDOL, Weight, Sector, Shares Held, Local Currency`; date line
  `As of 01-Oct-2026`; non-equity lines (cash, futures) have no SEC ticker.
- iShares IVV CSV was rejected: it returns an HTML page to scripts.
- EDGAR submissions carry `sic` and `sicDescription` (AAPL: 3571 Electronic
  Computers). SPY is a SEC registrant (CIK 884394), so its bars use the same
  tables as any company.

## 3. Storage (migration `2026-10-03_universe_l_v1.sql`)

- `ingestion_runs` provider check gains `SSGA` (constraint replaced, as in
  `ingestion_runs_status_check_v2`).
- `universe_snapshots(id, company_id → SPY, universe, holdings_as_of,
  observed_at, run_id)` and `universe_members(snapshot_id, position,
  source_ticker, name, identifier, sedol, weight, sector, shares_held,
  currency)`: literal holdings, append-only; a snapshot is stored once per
  `holdings_as_of`.
- `company_profiles(company_id, cik, sic, sic_description, observed_at,
  run_id)`: literal SIC from submissions, append-only, a new row only when the
  values change.

## 4. Ingestion

- `ingest_company` stores the profile from the submissions payload it
  already reads (`sec.company_profile`, no extra request).
- `python -m database.universe_v1 [--refresh-bars]`: downloads the SPY file
  (run `ssga.spy_holdings`), then for each equity member resolves its CIK from
  the SEC ticker file (fetched once), creates the company row if needed,
  stores its profile and its daily bars (`yfinance.daily_bars`), and finally
  SPY's own bars. Source tickers use dots (`BRK.B`); Yahoo and SEC use dashes
  (`BRK-B`). Members that cannot be resolved are listed, never guessed.

## 5. Calculations `l-relative-strength-v1`

- **Weighted score** (H2) on the `as_of`-basis close; needs 253 sessions and
  a last bar within 5 business days of `as_of`, otherwise the member is left
  out of the distribution (counted).
- **RS Rating:** percentile of the subject's score among the universe members
  other than itself: `p = (below + ½·ties) / n`, rating `= min(99, 1 + ⌊99·p⌋)`.
  Fewer than 400 scored members → `UNIVERSE_INCOMPLETE` (review). The subject
  need not be a member (e.g. RDDT, foreign filers).
- **Universe point in time:** the latest snapshot observed by `as_of`;
  members' bars observed by `as_of`. Known bias: today's constituents are
  used (survivorship), acceptable for live use, not for backtests.
- **Group:** SIC major group (first two digits; four-digit codes are mostly
  singletons in a 500-company universe). Group strength = median score of the
  group's members; groups with fewer than 3 scored members are not ranked.
  `group_rank_pct` = percentile of the group among ranked groups (100 = best);
  `rank_in_group_pct` = percentile of the subject's score within its group.
- **RS line:** subject close ÷ SPY close on common sessions, last 252:
  `rs_line_pct_below_high_52w`, `rs_line_new_high_recent` (last 20
  sessions); informative flag `RS_LINE_HIGH_BEFORE_PRICE` when the RS line is
  at a 52-week high and the price is more than 5% below its own.

## 6. Integrity

Foreign filer, split status needing review, inconsistent prices (price
series review reasons), stale prices, or an incomplete universe →
`REVIEW_REQUIRED`. Missing SIC or an unranked group →
`VERIFIED_WITH_PARTIAL_CORE_DATA`.

## 7. Contract `l-input-contract-v1`

`rs_rating`, `weighted_return_pct`, `universe_as_of`, `universe_scored`,
`sic`, `sic_description`, `group_key`, `group_size`, `group_rank_pct`,
`rank_in_group_pct`, `rs_line_pct_below_high_52w`, `rs_line_new_high_recent`,
`l_data_integrity`, `split_integrity_status`.

## 8. L Score v1 (approved 2026-10-03)

| Component | Max | Curve (input → points) |
|---|---|---|
| RS Rating | 50 | ≤50→0, 70→20, 80→32, 87→42, 90→46, 95→50 |
| Group rank (pct) | 20 | 0→0, 50→8, 80→16, 90→20 |
| Rank in group (pct) | 10 | 0→0, 50→4, 80→8, 100→10 |
| RS line: distance to 52w high (%) | 14 | 0→14, 5→10, 10→5, 20→0 |
| RS line: new high in last 20 sessions | 6 | yes → 6, no → 0 |

**Classic O'Neil result:** `INSUFFICIENT_DATA` without an RS Rating;
`FAIL_LAGGARD` when the RS Rating is below 80; `PASS` otherwise.
Status and usability as in A, N and S.

## 9. Implementation notes from real data (2026-10-03)

- **One member per issuer.** GOOG/GOOGL, FOX/FOXA and NWS/NWSA share a CIK; the
  second class found is skipped (`SHARE_CLASS_OF:<ticker>`) so an issuer never
  counts twice. Any error on one member is recorded and the run continues.
- **Universe members are not split-reconciled.** Only the evaluated companies
  have SEC facts and split captures; a member's series is used on its
  observation basis, and a member whose series shows a basis conflict or a
  suspected unrecorded split is left out of the distribution and counted
  (none on 2026-10-03).
- **The subject is excluded from its own distribution by ticker**, not by
  value, so a slightly different subject score (with split reconciliation)
  can never remove another member.
