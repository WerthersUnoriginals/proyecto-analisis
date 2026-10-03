# M — Market Direction Design (v1)

**Status:** Approved by the human on 2026-10-04: thresholds of §4 with
correction rule B, and the M Score v1 weights of §7. Implemented; live run in
`docs/audits/2026-10-04-m-live-market.md`.

**Date:** 2026-10-03

**Builds on:** the N price series (`price-series-v1`) and the S&P 500 universe
of L.

## 1. Human decisions (2026-10-03)

- **H1 — Indexes.** S&P 500 through the SPY ETF and Nasdaq-100 through the QQQ
  ETF (real traded volume, existing storage).
- **H2 — Scope.** Distribution days, follow-through days and the market state
  machine; plus, scored: each index against its 50- and 200-session moving
  averages, and breadth (share of S&P 500 members above their 50-session
  average).

M is market-wide: one result per `as_of`, the same for every company.

## 2. Benchmarks

SEC submissions list no ticker for the Invesco QQQ Trust, so benchmarks are an
explicit registry verified against SEC by CIK and name, never guessed:
`SPY → 0000884394 (SPDR S&P 500 ETF TRUST)`, `QQQ → 0001067839 (INVESCO QQQ
TRUST, SERIES 1)`. Their daily bars use `yfinance.daily_bars` as any company.
Ingestion: `python -m database.market_v1` (benchmarks' bars; members' bars come
from the universe ingestion of L).

## 3. Price series

Benchmarks and members are read with `price-series-v1` without split events
(ETFs: no split in the window; members: as in L). A series review reason
(basis conflict, suspected unrecorded split) excludes a member from breadth
and sends M to review when it affects a benchmark.

## 4. Calculations `m-market-direction-v1` (thresholds approved 2026-10-04)

Per index, over the last 300 sessions:

- **Distribution day:** close at least 0.2% below the previous close, on
  volume above the previous session's. It stays **active** for 25 sessions
  unless the index later closes 5% or more above that day's close.
- **Rally attempt:** while in correction, day 1 is the first up close after the
  lowest close of the correction; a close below that low restarts the count.
- **Follow-through day:** on day 4 or later of a rally attempt, a close at
  least 1.25% above the previous close, on volume above the previous
  session's.
- **State machine** (initial state from the close against the 200-session
  average 300 sessions back; the influence of the start fades well before
  `as_of`):
  - `CORRECTION` → `CONFIRMED_UPTREND` on a follow-through day;
  - `CONFIRMED_UPTREND` ↔ `UPTREND_UNDER_PRESSURE` when active distribution
    days reach 4 / fall below 4;
  - any uptrend → `CORRECTION` when the close is 8% or more below the highest
    close since the last follow-through day (rule B, chosen by the human:
    distribution days only put an uptrend under pressure; rule A, which also
    ended uptrends at 6 distribution days, called a correction with the S&P
    1% below its high);
  - a follow-through day starts a new uptrend and clears the distribution days
    counted before it.
- **Market state:** the worse of the two indexes (fail-closed), with each
  index reported.
- **Moving averages:** close vs 50- and 200-session simple averages (% above
  or below), per index.
- **Breadth:** percentage of scored S&P 500 members whose close is above
  their own 50-session average.

## 5. Integrity

A benchmark with fewer than 300 sessions, stale prices (more than 5 business
days), or a series review reason → `REVIEW_REQUIRED`. Breadth with fewer than
400 members → `VERIFIED_WITH_PARTIAL_CORE_DATA`.

## 6. Contract `m-input-contract-v1`

`market_state`, `state_by_index`, `state_since`, `last_follow_through_day`,
`distribution_days` (active, per index), `rally_day`, `pct_vs_ma50`,
`pct_vs_ma200` (per index), `breadth_pct_above_ma50`, `breadth_members`,
`m_data_integrity`.

## 7. M Score v1 (approved 2026-10-04)

| Component | Max | Curve (input → points) |
|---|---|---|
| Market state | 50 | confirmed uptrend 50, under pressure 25, correction 0 |
| Active distribution days (worst index) | 15 | 0→15, 2→12, 4→6, 6→0 |
| Moving averages | 20 | per index: above 50-session 5, above 200-session 5 |
| Breadth (% above 50-session) | 15 | ≤20→0, 40→6, 60→12, ≥70→15 |

**Classic O'Neil result:** `PASS` in a confirmed uptrend;
`FAIL_UPTREND_UNDER_PRESSURE`; `FAIL_MARKET_IN_CORRECTION`.

## 8. Implementation notes from real data (2026-10-04)

- **ETF volume inflates distribution days.** Over the same 25 sessions SPY
  shows 10 distribution days and the S&P 500 index with exchange volume
  (^GSPC) 6; QQQ 5 and the Nasdaq Composite 4. Counts are exact against Yahoo;
  the difference is the volume source (H1). Rule B keeps this from ending an
  uptrend on its own.
- **Follow-through resets the count.** Without it, QQQ fell back into
  correction the day after its 2026-08-04 follow-through day.
