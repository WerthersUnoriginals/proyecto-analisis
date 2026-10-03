# Technical Layer (Entry Phase Score v2) on 26 Companies

**Date:** 2026-10-04 (as_of 2026-10-03, last session 2026-10-02)

**Scope:** first live run of the port of the human's TradingView indicator
"Score Fase de Entrada v2" (`entry-phase-v2`, `t-input-contract-v1`), with
lookbacks as on the daily chart, next to the CAN SLIM composite. Reproduce
with `python -m database.t_v3_runner <tickers>` or `python -m database.batch_v3 <tickers>`.

## Port verification

- Indicators recomputed independently with pandas/numpy (rolling windows,
  Wilder averages, `polyfit`, `corrcoef`) on the same bars for NVDA, LRCX,
  AAPL, MSFT and XOM: largest relative difference 8.2e-14 (floating-point
  rounding only).
- Last close and 50-session average identical to a direct `yfinance` download.
- TradingView's own data feed can differ slightly (volume above all); the
  values should be checked once against the chart.

## Results

| Ticker | CAN SLIM | Technical score | Setup | Layer 1 |
|---|---|---|---|---|
| NVDA | 6/6, composite 87.06 | **5/5** | COMPRESSION_BASE | OK → **ENTRY_SETUP** |
| TSM | 3/6 (review) | 5/5 | COMPRESSION_BASE | OK |
| ASML | 3/6 (review) | 4/5 | COMPRESSION_BASE | OK |
| LRCX | 4/6 | 4/5 | COMPRESSION_BASE | OK |
| AAPL | 5/6 | 4/5 | COMPRESSION_BASE | OK |
| SMCI | 3/6 | 4/5 | TREND_CHANNEL | OK |
| XOM | 5/6 (I review) | 4/5 | COMPRESSION_BASE | OK |
| MSFT | 5/6 | 3/5 | COMPRESSION_BASE | OK |
| PLTR | 3/6 | 3/5 | TREND_CHANNEL | OK |

Fourteen companies score 0 (layer 1 fails: below the 200-session average or
50 below 200). CRWV has fewer than 430 sessions (insufficient history).

**Entry setups:** NVDA only — every CAN SLIM letter passes and the entry phase
is ready. The market (M) is under pressure, so the composite verdict stays
`CANDIDATE_MARKET_NOT_CONFIRMED`: the setup waits for a confirmed market.
