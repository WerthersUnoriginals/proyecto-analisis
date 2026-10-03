# CAN SLIM Composite Design (v1)

**Status:** Approved by the human on 2026-10-04, including the letter weights
and the watch threshold (§4). Implemented; live run in
`docs/audits/2026-10-04-canslim-composite-26-companies.md`.

**Date:** 2026-10-04

**Builds on:** the seven v1 letters (C v3 + C Score v1.3, A, N, S, L, I, M),
each with a contract, a graded score, a classic result and a data status.

## 1. Human decisions (2026-10-04)

- **H1 — Structure.** Both a classic checklist (how many of the six company
  letters pass, and whether all six do) and a weighted 0–100 composite of the
  six graded scores.
- **H2 — M as a buy filter.** M does not enter a company's composite; it gives
  the market signal: confirmed uptrend → buying allowed; under pressure →
  caution; correction → no buying (O'Neil: three out of four stocks follow the
  market).
- **H3 — Review.** The composite is computed over the available letters
  (renormalized), but a company with any letter in review or missing is
  flagged and can never be a checklist candidate (fail-closed without losing
  information).

## 2. Inputs per letter

| Letter | Classic pass | Data status field |
|---|---|---|
| C | `PASS` or `PASS_WITH_DECELERATION` | `data_integrity` |
| A | `PASS` | `annual_data_integrity` |
| N | `PASS` | `price_data_integrity` |
| S | `PASS` | `s_data_integrity` |
| L | `PASS` | `l_data_integrity` |
| I | `PASS` | `i_data_integrity` |

A letter is **in review** when its data status is `REVIEW_REQUIRED`, and
**missing** when it has no graded score. Partial statuses and C's accounting
difference are not review.

## 3. Result `canslim-composite-v1`

- `letters_passed` (0–6), `failed_letters`, `all_six_pass`.
- `composite_score`: weighted mean of the available graded scores, weights
  renormalized over available letters; `letters_available`.
- `data_status`: `REVIEW` (any letter in review or missing), `PARTIAL` (any
  partial), `OK`.
- `market_signal` from M: `BUY_ALLOWED`, `CAUTION`, `NO_BUY`, `UNKNOWN`.
- `verdict` (informative, not advice):
  - `CANDIDATE`: all six pass, data status not `REVIEW`, buying allowed;
  - `CANDIDATE_MARKET_NOT_CONFIRMED`: same, but the market is not confirmed;
  - `WATCH`: composite at or above the watch threshold otherwise;
  - `NOT_CANDIDATE`: the rest.

## 4. Weights and threshold (approved 2026-10-04)

| Letter | Weight |
|---|---|
| C — current earnings | 20 |
| A — annual earnings | 15 |
| N — new highs | 15 |
| S — supply and demand | 10 |
| L — leader (RS) | 25 |
| I — institutional sponsorship | 15 |

Earnings (C + A = 35) and relative strength (L = 25) carry the most weight,
as in IBD's Composite Rating. Watch threshold: composite ≥ 70.

## 5. Delivery

`canslim_score_v1.py` (pure), `batch_v3` gains the composite per company, a
ranking by composite and the market signal in the summary; live run on the
26 companies and an audit note.
