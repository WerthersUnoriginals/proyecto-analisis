# Technical Layer — Entry Phase Score v2 Design

**Status:** Approved by the human on 2026-10-04 and implemented. Thresholds
are the inputs of the human's own TradingView indicator; no new weights. Live
run in `docs/audits/2026-10-04-technical-entry-phase-26-companies.md`.

**Date:** 2026-10-04

**Reference:** `docs/reference/score_fase_entrada_v2.pine` ("Score Fase de
Entrada v2 - Trend Following", Pine Script v6, provided by the human).

## 1. Human decisions (2026-10-04)

- **H1 — Lookbacks as in TradingView.** On the daily chart
  `timeframe.in_seconds()` is 86,400, so `f_barsForDays(d)` = d bars: one
  "day" is one session (42 → 42 sessions, 365 → 365 sessions). The port
  reproduces this exactly so values can be checked against the chart. (The
  indicator's intent of calendar days would be 42 → ~29 and 365 → ~252
  sessions; not adopted.)
- **H2 — Entry-timing layer.** The technical score is computed per company and
  shown next to the CAN SLIM result without changing the composite. A CAN
  SLIM candidate (all six letters pass) with layer 1 OK and a technical score
  of 4–5 is flagged `ENTRY_SETUP`: CAN SLIM says what, the technical layer
  says when.

## 2. Port `entry-phase-v2` (daily bars, `as_of` basis)

Inputs: the N price series (open, high, low, close, volume on the `as_of`
split basis). Pine built-ins are reproduced:

| Pine | Python |
|---|---|
| `ta.sma(x, n)` | mean of the last n values |
| `ta.highest/lowest(x, n)` | max/min of the last n values, current included |
| `ta.percentrank(x, n)` | % of the previous n values (current excluded) ≤ current |
| `ta.atr(n)` | Wilder RMA of the true range, seeded with the SMA of the first n |
| `ta.rsi(x, n)` | Wilder RMA of gains and losses; 100 when there are no losses |
| `ta.linreg(x, n, off)` | least-squares line over the last n values at position n−1−off |
| `ta.correlation(bar_index, x, n)` | Pearson correlation over the last n values |
| `ta.stdev(x, n)` | population standard deviation |
| `x[k]` | value k bars back |

- **Layer 1:** `close > MA200 and MA50 > MA200`.
- **Layer 2 (0–5):** cond1 amplitude percentile ≤ 40 (42-bar range,
  365-bar percentile); cond2 ATR(14) below its value 14 bars back; cond3
  position in the 42-bar range > 0.66; cond4 SMA5(volume) ≥ 0.8 × SMA20;
  cond5 distance to the 365-bar highest high in [0%, 3%). `score_final` = score
  if layer 1 is OK, else 0.
- **Setup:** `TREND_CHANNEL` when the 60-bar regression has R² ≥ 0.7 and a
  positive slope; else `COMPRESSION_BASE` when cond1 holds; else
  `NO_CLEAR_STRUCTURE`. Channel bounds: regression value ± 2 standard
  deviations.
- **Momentum (informative):** `N/A` unless the close is within 1% of the
  60-bar highest high; then `CONFIRMED` if RSI(14) is within 5 points of its
  60-bar maximum, else `DIVERGENT`.
- **Temporal state:** `score_final` for each of the last 10 sessions and the
  sessions since it was last at 4 or more.

## 3. Integrity and contract `t-input-contract-v1`

Fewer than 430 bars (365-bar percentile of a 42-bar range plus warm-up) →
`INSUFFICIENT_HISTORY`; stale prices (more than 5 business days) or a price
series review reason → `REVIEW_REQUIRED`. Fields: `layer1_ok`, the five
conditions, `score`, `score_final`, `setup_type`, `momentum`, the underlying
values (MAs, range, amplitude and its percentile, ATR now/past, position,
volume means, reference high and distance, channel R²/slope/bounds, RSI),
`score_history`, `sessions_since_score_4`, `t_data_integrity`.

## 4. Delivery

`entry_phase_v2.py` (pure port), `t_contract_v1.py`, `t_v3_runner.py`,
`batch_v3` (technical columns and the `ENTRY_SETUP` flag), Experience Store
payload, a cross-check of indicator values against an independent
computation, and a live run.

## 5. Implementation notes (2026-10-04)

- Indicators are floats, as in TradingView; prices stay `Decimal` in the
  evidence and are converted only inside the port.
- Verified against an independent pandas/numpy computation (relative
  difference below 1e-13) and against Yahoo closes.
- The Experience Store payload includes the technical contract from now on,
  so future calibration can test whether `ENTRY_SETUP` adds to the composite.
