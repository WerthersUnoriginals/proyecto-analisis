# C Score v1.3 Design

**Status:** Approved for implementation by the human (2026-10-02, "c_score_v1
must be fixed"). Weights, knots, thresholds, and classes are unchanged.

**Base:** `c_score_v1.py` (model `1.2-exp`), which stays byte-identical as the
legacy score. v1.3 lives in `c_score_v13.py` and is consumed by C v3.

## 1. Defect

v1.2 sees only computable YoY percentages. A YoY is not computable when the
prior-year EPS is ≤ 0, so a company that keeps losing money, or whose losses
widen, gets its EPS components marked *unavailable* instead of *unfavorable*.
The total is then renormalized over the remaining components (sales), which
can give a loss-making company a high normalized score. Trend and persistence
also silently skip loss quarters and use older quarters instead. This
violates "missing data ≠ unfavorable data".

## 2. Quarter status

C v3 adds `eps_growth_detail` to its contract (an extra key; the eleven inputs
are unchanged). Each quarter has one status:

| Status | Meaning |
|---|---|
| `GROWTH` | YoY computed (it can be negative, including a fall into loss from a positive base). |
| `LOSS` | current EPS ≤ 0 and YoY not computable. Known and unfavorable. |
| `LOSS_TO_PROFIT` | prior EPS ≤ 0 and current EPS > 0. Known improvement without a percentage. |
| `NO_DATA` | no value, or no same-source/same-concept comparable. Unknown. |

The detail also carries the latest quarter's comparable EPS (the real prior
value, so small-base risk no longer infers it).

## 3. Rules (changes from v1.2 only)

- **EPS growth (35):** `LOSS` → 0 points, *available*. `LOSS_TO_PROFIT` →
  unavailable with status `LOSS_TO_PROFIT`. The classic result still passes,
  but the score is partial and goes to review: a percentage is not invented.
  `NO_DATA` → unavailable.
- **EPS acceleration (10):** latest `LOSS` → 0 points, available. Latest
  `GROWTH` with previous `LOSS` or `LOSS_TO_PROFIT` → unavailable, and
  `BASE_EFFECT_RISK` is raised when latest YoY ≥ 25.
- **Trend quality and persistence (10 + 10):** use the last five fiscal
  quarters, drop `NO_DATA`, keep the last four, and require at least three.
  `LOSS` counts as known and unfavorable: it is not positive, not ≥ 10, and not
  ≥ 25; in trend it is below any growth value. `LOSS_TO_PROFIT` counts as
  positive but not strong. The range check (≤ 50 pp) uses `GROWTH` values only.
  With four `GROWTH` quarters, the result equals v1.2.
- **Small-base risk:** uses the real comparable EPS when present, otherwise the
  v1.2 inference.
- **Without detail** (a legacy report): a latest EPS ≤ 0 with no YoY is `LOSS`;
  otherwise v1.2 behavior.

## 4. Acceptance

- When every recent quarter is `GROWTH` (AAPL, MSFT, NVDA on 2026-10-02),
  v1.3 gives the v1.2 score exactly.
- Persistent and widening losses score low and usable, not partial and high.
- Loss-to-profit stays partial and under review, with explicit status.
- Every v1.2 test stays green; v1.3 has its own tests.
