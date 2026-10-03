# Experience Store Design (v1)

**Status:** Approved by the human on 2026-10-04 (scope, cadence and outcome
measures). No score weights are involved.

**Date:** 2026-10-04

**Builds on:** the seven v1 letters, the CAN SLIM composite, `batch_v3`, and
the price series of N.

## 1. Purpose and constraint

Record what the system said, then measure what happened, so that scores can
be recalibrated with evidence (always by human decision).

Point in time is "known to this database" (`observed_at`, decision D1): an
evaluation for a past `as_of` sees nothing downloaded later, so history cannot
be backtested. The Experience Store therefore records **forward**: each
snapshot is evidence of a real evaluation at the time it was made.

## 2. Human decisions (2026-10-04)

- **H1 — Content.** Everything, literally: the contracts and scores of the
  seven letters and the composite, as immutable JSON, with every calculation
  version, so a past verdict can be audited and alternative weights tested on
  the exact inputs.
- **H2 — Cadence.** Snapshots are taken by an explicit command, never as a
  side effect of a batch run.
- **H3 — Outcomes.** Forward returns, absolute and against SPY; worst drawdown
  and O'Neil's 8% stop versus a 20% gain; a calibration report.

## 3. Storage (migration `2026-10-04_experience_v1.sql`)

- `experience_snapshots(id, as_of, label, code_commit, market, created_at)`:
  one row per snapshot; `code_commit` is the git HEAD when it was taken.
- `experience_records(snapshot_id, company_id, ticker, composite_score,
  verdict, letters_passed, data_status, entry_bar_date, entry_close, payload)`:
  one row per company; `payload` holds the full letter results.
- Append-only. Outcomes are **not stored**: they are derived on read from the
  price evidence visible at report time, so a later correction of prices is
  never frozen into a stored outcome.

## 4. Commands

- `python -m database.experience_v1 snapshot AAPL NVDA … [--file f]
  [--label weekly]`: evaluates every letter at `as_of` = now (from stored
  evidence; ingest first with `batch_v3 --ingest` if wanted) and stores the
  snapshot in one transaction.
- `python -m database.experience_v1 report [--as-of ISO]`: outcomes per record
  and the calibration tables.

## 5. Outcomes `experience-outcomes-v1`

For each record, on the price series built at report time (one split basis
for entry and exit):

- **Entry:** the last session on or before the snapshot `as_of`.
- **Horizons:** 21, 63, 126 and 252 sessions after entry (≈ 1, 3, 6, 12
  months); a horizon is `PENDING` until it has elapsed.
- **Per horizon:** return (close to close), SPY return over the same dates,
  excess return, maximum drawdown (lowest low) and maximum gain (highest
  high), both from the entry close.
- **O'Neil rule (within 252 sessions):** `STOP_FIRST` if a low reaches −8%
  before a high reaches +20%, `TARGET_FIRST` for the opposite,
  `NEITHER`/`PENDING` otherwise; both on the same session counts as
  `STOP_FIRST` (conservative).

## 6. Calibration report

Matured outcomes grouped by verdict and by composite bucket (< 40, 40–55,
55–70, ≥ 70), per horizon: count, mean return, mean excess return, share with
positive excess, and stop/target shares. The report informs; it never changes
weights.

## 7. Implementation notes (2026-10-04)

- **Baseline snapshot.** `baseline-2026-10-04` (snapshot 2, as_of
  2026-10-03T22:28Z): 26 records, ~280 KB of payload, market under pressure.
  First horizon outcomes mature after 21 sessions (early November 2026).
- **Code version.** `code_commit` is the git HEAD, suffixed `-dirty` when
  tracked files had uncommitted changes. The baseline was taken before the
  Experience Store itself was committed (HEAD `53f3d91`, letter code
  unchanged since).
- Snapshot ids come from an identity sequence, so a rolled-back test insert
  can leave a gap (the baseline is snapshot 2).
