# Annual Earnings Architecture Design

**Status:** Superseded on 2026-10-02 by `2026-10-02-annual-earnings-v3-design.md`.
Kept as history of the Codex draft; do not implement from this document.

**Original status:** Draft for human review

**Date:** 2026-09-28

**Base commit:** `4bb7a81f4efec8cccbcd8c37f7db8c78920931ad`

**Scope:** A — Annual Earnings

**Out of scope:** implementation, migrations, database deployment, provider calls, scoring weights and thresholds

## 1. Purpose

This specification defines how CAN SLIM+ will represent, normalize, select,
and expose annual fundamental evidence for the future A — Annual Earnings
block.

The design reuses the existing append-only `fundamentals_raw` and
`fundamentals_normalized` architecture. It does not create a parallel annual
storage stack. Annual evidence receives explicit temporal semantics and a
separate versioned effective policy.

This specification defines a data contract before defining an A score. It does
not authorize any formula, weight, threshold, migration, provider request, or
PostgreSQL change.

## 2. Scope

### 2.1 In scope

- explicit temporal classification of normalized observations;
- coexistence of quarterly Q4 and annual FY evidence;
- SEC annual identity, filing resolution, amendments and restatements;
- point-in-time reconstruction;
- source priority and source isolation;
- the pre-score A data contract;
- fail-closed behavior;
- compatibility rules that preserve the closed C block;
- fixtures and acceptance gates for later implementation.

### 2.2 Out of scope

- changes to `fundamental_c.py`, `c_score_v1.py`, or
  `database/c_fundamentals_adapter.py`;
- changes to `c-v2.6-compatible-v1`;
- changes to corporate actions or split integrity;
- score weights, score thresholds, rating classes, or automatic decisions;
- annual Yahoo acquisition in the first SEC implementation;
- deriving annual facts from quarterly observations;
- deriving Q4 from annual minus year-to-date facts;
- changing or deleting historical migrations;
- deployment to PostgreSQL.

## 3. Starting state

The C — Current Earnings block is closed and is a stable dependency of A.
Repository-backed baselines are:

- `sec_effective = 75`;
- `hybrid_effective = 60`;
- the authoritative C input contract contains exactly 11 inputs.

No other effective-row count is a contractual C baseline unless a future test
establishes it explicitly.

The current AAPL persistence state examined during the architecture audit is:

- 135 SEC raw observations, all quarterly-duration facts;
- 30 Yahoo raw observations, all from quarterly datasets;
- 330 normalized v1/v2 observations;
- zero persisted annual-duration observations.

The current SEC importer calls `_quarter_like_fact()` before persistence and
therefore does not store annual Company Facts. The current Yahoo importer uses
`yahoo.fundamentals_timeseries` quarterly types and
`yfinance.quarterly_income_stmt`. Existing persistence cannot reconstruct the
missing annual history without a new acquisition.

`annual_comparisons_by_source()` remains part of C. It finds a quarterly
observation approximately one year before another quarterly observation. It is
not an Annual Earnings implementation and must not be reused as one.

## 4. Architecture

Annual Earnings reuses this pipeline:

```text
provider evidence
  -> fundamentals_raw
  -> versioned normalization into fundamentals_normalized
  -> a-annual-v1 effective selection
  -> pre-score A contract
  -> future A scoring policy
```

The shared layers remain source-preserving and append-only. Annual and
quarterly observations coexist as separate normalized observations. Selection
policies, not destructive rewrites, decide which observations a consumer may
use.

The future implementation shall introduce only the minimum shared schema
extension required to make period semantics explicit. It shall not add
`fundamentals_annual_raw` or `fundamentals_annual_normalized` tables.

## 5. Explicit temporal semantics

`fundamentals_normalized` shall gain a required `period_type` field with this
closed initial domain:

### 5.1 `QUARTERLY`

A duration observation representing one fiscal quarter.

For SEC, ordinary quarterly facts have a supported form (`10-Q`, `10-Q/A`,
`10-K`, or `10-K/A`), a valid start/end interval, and a duration from 70
through 110 days inclusive. A Q4 fact reported in a `10-K` or `10-K/A` remains
`QUARTERLY` when its interval is quarterly. `fp=FY` and form type alone do not
make the fact annual.

For Yahoo, an observation from an explicitly quarterly dataset may be
`QUARTERLY` even when the provider omits `period_start`, provided its dataset
variant and fiscal identity satisfy the approved quarterly normalizer contract.

### 5.2 `ANNUAL`

A reported duration observation representing one complete fiscal year.

For SEC v1, `ANNUAL` requires all of the following:

- form `10-K` or `10-K/A`;
- fiscal-period metadata `FY`;
- demonstrable `fiscal_year`;
- non-null `period_start` and `period_end`;
- an interval of 330 through 400 days inclusive for the ordinary annual v1
  class;
- compatible tag, unit, currency, and metric semantics;
- a non-ambiguous fiscal identity.

The ordinary structural duration band for A v1 is 330 through 400 days
inclusive. This includes normal 52-week and 53-week fiscal years. Duration is a
validation condition, never the sole fiscal-identity rule.

### 5.3 `OTHER_DURATION`

A valid duration fact that is neither a supported quarter nor a supported
ordinary fiscal year. Examples include year-to-date, trailing-twelve-month,
stub, and transition periods.

`OTHER_DURATION` observations remain auditable but are ineligible for
`a-annual-v1` and `c-v2.6-compatible-v1`.

### 5.4 `INSTANT`

An observation measured at a date rather than over a start/end interval, such
as a future balance-sheet equity fact. `INSTANT` is reserved for metrics whose
approved semantic contract is point-in-time. None of the A v1 minimum EPS
inputs is `INSTANT`.

### 5.5 `UNRESOLVED`

Evidence whose temporal type cannot be demonstrated because dates, provider
semantics, or fiscal metadata are missing or contradictory.

`UNRESOLVED` requires intrinsic review and is ineligible for every automatic
effective policy. It must never be coerced to `QUARTERLY` or `ANNUAL` through
date proximity alone.

### 5.6 Classification precedence

1. An approved instant metric with a valid instant date is `INSTANT`.
2. A demonstrated quarterly observation is `QUARTERLY`.
3. A demonstrated complete fiscal-year observation is `ANNUAL`.
4. A valid but unsupported duration is `OTHER_DURATION`.
5. Missing or contradictory evidence is `UNRESOLVED`.

Form, `fp`, duration, or calendar year alone never determines the class.

### 5.7 Normative storage-consistency matrix

The normalizer assigns `period_type`; PostgreSQL validates that the assigned
class is internally consistent. Database constraints never infer a temporal
class from NULL, dates, form, duration, or fiscal metadata.

| `period_type` | Required stored consistency | Automatic-policy eligibility |
|---|---|---|
| `QUARTERLY` | `source_period_end` is non-null; `fiscal_quarter` is 1–4; `fiscal_year` and `canonical_period_end` are non-null for an eligible observation. SEC requires non-null `source_period_start` and a valid ordered interval. Yahoo may omit `source_period_start` only for an approved explicitly quarterly variant. | Eligibility still requires the applicable quarterly policy; the class alone never grants eligibility. |
| `ANNUAL` | `source_period_start`, `source_period_end`, `canonical_period_end`, and `fiscal_year` are non-null; start is not after end; `fiscal_quarter IS NULL`; source/variant and annual identity are demonstrated by an approved annual normalizer. | Only an approved annual policy may select it. |
| `INSTANT` | `source_period_end` is the measurement date; `source_period_start IS NULL`; `series_date=source_period_end`; `fiscal_quarter IS NULL`. Fiscal metadata may describe the filing context but never establishes instant temporal semantics. | Eligible only for a metric and policy whose versioned contract explicitly admits instant facts. |
| `OTHER_DURATION` | Both period dates are non-null and ordered; the normalizer records the reason the interval is neither supported quarterly nor supported annual. | Ineligible for `c-v2.6-compatible-v1` and `a-annual-v1`. |
| `UNRESOLVED` | Known source dates are preserved, but temporal identity is not demonstrated; `intrinsic_quality_status` is at least `REVIEW_REQUIRED`; `selection_eligibility='INELIGIBLE'`. | Ineligible for every automatic policy. |

The migration shall implement the closed `period_type` domain and only those
cross-field checks that can be proven from stored columns. Policy-specific
facts such as approved variants, semantic tag classes, and duration bands are
validated by versioned Python normalizers and tests, not duplicated as brittle
DDL inference. A disagreement between the Python validator and PostgreSQL is a
hard failure.

## 6. Compatibility with the 330 existing normalized rows

The number `330` is only the currently observed AAPL baseline: 165 normalized
v1 rows plus 165 normalized v2 rows. It is not a universal database
precondition. A future deployment must inspect the actual population and must
not assume that another company or installation contains 330 rows.

The A1.1 gate shall retain an AAPL manifest with, at minimum, these columns:

```text
company_id, source, source_variant, normalizer_version, expected_period_type,
row_count
```

The checked-in repository gate proves the current 165/165 split in
`database/backfill_semantics_v2.py`; a deployment gate must additionally
compare the live manifest before and after classification.

The temporal rollout shall be fail-closed and phase-transactional: every DDL or
data phase has its own atomic boundary, while application deployment is an
explicit capability transition between phases. Its compatible writer
transition is mandatory:

1. add `period_type` as nullable, with no database default;
2. extend the shared Python model and write path before imposing `NOT NULL`;
3. make every existing quarterly normalizer write
   `period_type='QUARTERLY'` explicitly;
4. reserve `period_type='ANNUAL'` for future annual normalizers;
5. backfill existing rows only from an approved source/version manifest;
6. abort if any row is unmatched or remains NULL;
7. validate all domain and cross-field constraints;
8. impose `NOT NULL` only after steps 1–7 succeed.

There must be no permanent `QUARTERLY` database default, no conversion of a
NULL value to `QUARTERLY`, and no `NOT NULL` constraint before the write path
has been updated.

All 330 existing observations were produced by the approved quarterly
normalizer versions and datasets. They shall be classified as `QUARTERLY` only
when their existing version/source contract proves that classification:

- SEC v1/v2 observations must satisfy the existing quarterly SEC contract;
- Yahoo v1/v2 observations must identify an approved quarterly variant;
- ineligible `EPS_UNSPECIFIED` rows from the historical quarterly yfinance
  dataset remain `QUARTERLY` and remain ineligible;
- quality, eligibility, identity, values, dates, raw lineage, and versions are
  unchanged.

The migration shall abort if any existing row cannot be deterministically
mapped. It shall not assign `UNRESOLVED` merely to complete the migration.

Required migration gates for the current AAPL baseline are:

- the manifest's 330 rows before and after temporal classification;
- no duplicate, deleted, or rewritten normalized observation;
- all existing rows have `period_type='QUARTERLY'`;
- `sec_effective=75` remains unchanged;
- `hybrid_effective=60` remains unchanged;
- the exact 11-input C contract remains unchanged;
- the 37-column `fundamentals_effective_current` contract remains unchanged.

`period_type` shall not be exposed through the existing C view. C obtains
temporal isolation through its pinned normalizer versions, required fiscal
quarter, and unchanged selection policy.

`NormalizedObservation.period_type` is appended after the existing dataclass
fields with a default of `None`, so no existing positional slot is shifted; all
persisting constructors must nevertheless pass it by keyword. This does not
change the C adapter contract. The C adapter continues to read the explicit
37-column C projection, which does not contain `period_type`; its transient
objects may carry `period_type=None` for backwards-compatible construction,
but NULL is never persisted or interpreted as `QUARTERLY`. Quarterly and
annual writers must pass the value explicitly. This leaves
`database/c_fundamentals_adapter.py` unchanged while making any persisted
observation without an explicit temporal type fail closed. Such a transient C
projection is not a normalized observation and is outside the invariant in §16.

### 6.1 PostgreSQL 17 and application rollout protocol

Application deployment and PostgreSQL DDL are distinct actors. A1.1 shall
produce a versioned, rerunnable state machine; it shall not pretend that Python
code can be deployed inside a database transaction.

The new application build advertises the immutable capability
`normalized-period-type-writer-v1` in its deployment metadata and PostgreSQL
`application_name`. It is dual-schema-aware during this rollout only:

- against the exact old schema it uses the legacy insert projection because
  the column does not exist;
- once the nullable column exists it always supplies an explicit
  `period_type`, and quarterly writers supply `QUARTERLY`;
- it refuses normalized writes against an unknown or divergent schema state.

The deployment inventory and active writer sessions must agree on build ID and
capability. An unrecognized session, a writer without the capability, or a
writer that inserts NULL after the capability gate aborts progression. Merely
assuming that old processes have exited is not sufficient.

#### Phase 0 — preflight

1. Verify the expected application build, enumerate all processes/jobs capable
   of normalized or raw writes, and record their build/capability identifiers.
2. Verify exact catalog signatures for `fundamentals_raw`,
   `fundamentals_normalized`, their constraints and indexes, foreign keys, and
   the 37-column C view. Abort on missing, extra, or divergent objects.
3. Record the real population manifest and the AAPL regression manifest. Record
   the C gates `75`, `60`, and the exact 11 inputs. AAPL counts are not universal
   database preconditions.
4. Set bounded deployment-specific `lock_timeout` and `statement_timeout`.
   Timeout is an abort signal, never permission to continue partially.

#### Phase 1 — nullable expansion

1. In a short transaction, add nullable `period_type` with its closed-domain
   and storage-consistency checks as `NOT VALID` where PostgreSQL permits.
2. Do not add a default and do not set `NOT NULL`.
3. Verify the exact catalog state and commit. Old writers may still operate in
   this phase; any rows they create have NULL and are included in the later
   manifest/backfill gate.

#### Phase 2 — writer capability transition

1. Deploy the dual-schema-aware application build.
2. Verify `normalized-period-type-writer-v1` in the deployment inventory and
   active database sessions.
3. Stop, drain, or hard-block every old writer. If an old writer appears or a
   NULL is inserted after this gate, abort and return to Phase 2; do not advance
   to `NOT NULL`.
4. Prove with a rollback-only write-path test that every quarterly normalizer
   supplies `QUARTERLY` explicitly. Future annual writers must supply `ANNUAL`.

This defines both compatibility edges: new application plus old schema is
supported only through the exact legacy projection above; old application plus
new nullable schema remains temporarily writable but cannot pass the Phase 2
capability gate. Old application plus `NOT NULL` schema is prohibited.

#### Phase 3 — backfill and validation

1. With incompatible writers drained, acquire only the bounded locks needed to
   stabilize normalized writes.
2. Backfill NULL rows from the approved source/variant/normalizer manifest.
   Abort on unmatched rows; never infer `QUARTERLY` from NULL alone.
3. Verify that values, lineage, eligibility, quality, versions, and row counts
   are unchanged apart from `period_type`.
4. Validate the domain and cross-field constraints using `VALIDATE CONSTRAINT`.
5. Prove zero NULL values, recheck the capability gate, then set `NOT NULL` in
   a short transaction. A timeout or failed assertion rolls back that phase.

#### Phase 4 — raw-index replacement and postflight

1. Create `fundamentals_raw_dedup_unique_v2` while the old index remains. Use
   the PostgreSQL 17 mode appropriate to the deployment (`CONCURRENTLY` when
   writers cannot be held for the build); do not place a concurrent index build
   inside a transaction block.
2. Validate its exact catalog definition, uniqueness, lookup behavior, and row
   coverage. An invalid concurrent-index artifact is a known partial state and
   must be removed/rebuilt only by the explicit recovery branch.
3. In a short locked transaction, recheck signatures and remove the old index.
   No annual acquisition may start before this commit.
4. Re-run the population manifest, AAPL manifest, 37-column view contract,
   `75/60/11` C gates, raw-idempotency tests, and normalized-write tests.
5. Resume writers only after postflight passes.

The rollout recognizes only the exact states `OLD`, `PERIOD_TYPE_NULLABLE`,
`PERIOD_TYPE_VALIDATED`, and `COMPLETE`. A rerun from one of those states first
revalidates every completed phase and continues at the next boundary. An exact
`COMPLETE` state is a verified no-op. Any other partial or divergent state
aborts with recovery instructions; `IF NOT EXISTS` alone is not idempotency.

### 6.2 Rollback and forward recovery

Before adoption—while no non-quarterly normalized row, no Q4/FY raw pair, and
no writer requiring the new contract exists—the rollout may reverse the index
swap and remove the nullable temporal extension under the same signature and
capability gates.

After any valid Q4/FY coexistence pair, any non-quarterly normalized evidence,
or any deployed writer that requires the new contract, the migration is
**FORWARD-ONLY**. A post-commit failure is repaired by completing or correcting
the forward migration. The old raw key must never be recreated when it would
collapse valid period-start-distinct evidence. Application rollback in that
state may use only a build that understands the new schema; it may not revive
an old incompatible writer.

## 7. Raw identity and Q4/FY coexistence

The deployed unique index `fundamentals_raw_dedup_unique` currently identifies
raw evidence by:

```text
company_id
source
metric
period_end
xbrl_tag
filed_date
source_record_id
```

It omits `period_start`. An annual FY fact and a quarterly Q4 fact can share
every field above while differing in start date and value. The current index
would make these two distinct SEC facts collide.

The future migration shall replace the identity with the same fields plus
`period_start`:

```text
company_id
source
metric
period_start
period_end
xbrl_tag
filed_date
source_record_id
```

For PostgreSQL 17, the required A1.1 physical index uses
`NULLS NOT DISTINCT` and
preserves the current NULL/empty-string behavior through explicit expressions:

```sql
CREATE UNIQUE INDEX fundamentals_raw_dedup_unique_v2
ON fundamentals_raw (
    company_id,
    source,
    metric,
    period_start,
    period_end,
    NULLIF(xbrl_tag, ''),
    filed_date,
    NULLIF(source_record_id, '')
) NULLS NOT DISTINCT;
```

`NULLS NOT DISTINCT` makes two absent `period_start` values the same logical
absence and avoids a sentinel date. `NULLIF` retains the deployed index's
existing equivalence between NULL and empty text. If the target PostgreSQL
signature does not support this exact expression form, the migration must
abort rather than silently fall back to a different identity; an explicitly
tested equivalent expression is required.

The logical identity is the complete tuple above. `source_variant` is also a
semantic identity attribute when a provider has multiple datasets. The current
raw table does not persist it as a separate column: SEC has the canonical
variant `sec.company_facts`, while current Yahoo `source_record_id` values are
variant-scoped content identities. A1.1 must verify that invariant in the raw
payload/writer and abort if two variants can share one raw key. A future source
whose provider identity is not variant-scoped requires an additive raw-column
migration before ingestion; it must not be mixed silently.

The SEC `source_record_id` remains the real accession and must not be decorated
with a period or hash merely to evade the index. Physical raw identity and
provider identity remain separate concepts.

The lookup/retry path must use the same complete identity as the index,
including `period_start`, with NULL-safe equality. After locating a candidate,
the writer compares the following `raw-identity-v1` semantic field set:

```text
company_id, source, source_variant, metric, period_start, period_end,
xbrl_tag/source_metric_name, filed_date, source_record_id, value, unit,
currency, fiscal_year, fiscal_quarter, form_type, semantic_payload
```

`semantic_payload` is canonical JSON with transport fields such as fetch time
removed and provider-specific semantic fields retained. The SEC allowlist is
`start,end,fy,fp,form,accn,frame,val,unit`; Yahoo retains
`provider_id,source_variant,source_metric_name,source_unit,source_scale_factor,
unit,currency,period_start,period_end,fiscal_year,fiscal_quarter,value`.
The allowlist itself is versioned with `raw-identity-v1`.

The canonical representation is `raw-semantic-canonical-v1`:

- booleans are distinct from integers;
- integers use base-10 digits with no leading plus sign;
- `Decimal` and finite floats are converted to their exact decimal value,
  written without exponent, trailing fractional zeroes, or negative zero;
  binary floats enter through their shortest round-trip decimal string;
- `NaN`, positive infinity, and negative infinity are invalid;
- dates use `YYYY-MM-DD`;
- timezone-aware datetimes are converted to UTC and emitted as RFC 3339 with
  six fractional digits and `Z`; naive datetimes are invalid;
- strings are Unicode NFC and otherwise preserved exactly; whitespace and the
  empty string remain significant and distinct from NULL;
- JSON null is a tagged null value; an absent allowlisted field is a distinct
  tagged missing value;
- list order is preserved; v1 has no implicitly set-like list;
- dictionary keys are NFC strings, sorted by Unicode code point, and duplicate
  normalized keys are invalid;
- canonical JSON uses UTF-8, sorted keys, no insignificant whitespace, and no
  ASCII escaping requirement.

The Yahoo projection reads the approved paths from the nested
`source_payload.semantic_payload` plus the explicitly allowlisted top-level
provider fields. If a value is duplicated in both locations, both must agree
canonically. The SEC projection reads the approved Company Facts fields from
the retained payload. Unknown and transport-only fields do not participate;
changing an allowlist requires a new canonical version.

Implementations compare the canonical bytes and may also record
`raw-semantic-canonical-v1:sha256:<lowercase-hex>` as a reproducible diagnostic
fingerprint. The fingerprint never replaces the complete semantic comparison.

An identical retry returns the same `raw_id` and performs no update. The same
logical identity with any incompatible semantic field raises the stable error
`RAW_IDENTITY_CONFLICT`; it never returns a previous incompatible row. Current
Yahoo content-addressed IDs intentionally create a new raw identity when the
provider changes the semantic value, preserving append-only snapshots rather
than pretending that a changed payload is an identical retry.

### 7.1 Concurrent raw writes

The raw writer contract uses PostgreSQL `READ COMMITTED` and the unique index
as the per-key arbiter; it does not take a table-wide or application-wide lock.

For two concurrent inserts of canonically identical evidence:

1. both execute `INSERT ... ON CONFLICT DO NOTHING RETURNING id` with the same
   complete identity;
2. one inserts; the other waits for resolution of the conflicting transaction;
3. a writer that receives no ID performs a new-statement, NULL-safe `SELECT`
   using the exact index identity, compares canonical semantics, and returns
   the existing `raw_id`;
4. exactly one row exists and both calls resolve to that ID.

For the same logical identity with incompatible canonical semantics, the first
committed row remains and the other writer locates it and raises
`RAW_IDENTITY_CONFLICT`. It must not update, replace, or silently return it.

SQLSTATE `40001` or `40P01`, connection loss before commit outcome is known, or
an absent row after a conflict-resolution lookup causes a bounded retry of the
whole raw-write transaction with the same immutable evidence. Each retry first
performs the complete lookup and semantic comparison. The retry budget and
backoff are operational configuration, but exhaustion returns a distinct
transient write error; it never becomes `RAW_IDENTITY_CONFLICT`. The
concurrency contract requires real PostgreSQL integration tests for identical
and incompatible races in addition to offline tests.

No migration is authorized by this specification.

## 8. SEC annual normalization

### 8.1 Version

The first annual SEC normalizer is conceptually named:

```text
sec-annual-normalized-v1
```

It is append-only and does not change `sec-normalized-v1` or
`sec-normalized-v2`.

### 8.2 Preserved evidence

Each normalized annual observation preserves:

- source and `source_variant='sec.company_facts'`;
- metric and original XBRL tag;
- value, source unit, normalized unit, currency, and scale;
- original `period_start` and `period_end`;
- `canonical_period_end`;
- `fiscal_year` and `fiscal_quarter=NULL`;
- form type and filing date through raw lineage;
- accession through `raw_id -> fundamentals_raw.source_record_id`;
- `observed_at` from the raw acquisition;
- `source_available_at` only when demonstrable;
- normalizer version, intrinsic quality, eligibility, and reasons.

`series_date` remains the original `source_period_end` for A v1.

### 8.3 Fiscal-year identity

Fiscal-year identity is determined from the combined evidence of form, fiscal
metadata, dates, filing history, and company fiscal calendar. It is not derived
from natural year and is not derived from duration alone. The versioned
`sec-annual-identity-v1` algorithm is:

1. PIT-filter all facts and supporting filings before grouping them.
2. Group candidates by company, semantic metric/tag class, unit, currency, and
   exact `period_start`/`period_end`. Duration only rejects an impossible class;
   it does not assign FY.
3. An **original presentation** candidate is a visible `10-K` (not `10-K/A`)
   whose own fact has `fp='FY'`, a non-null `fy`, the exact group interval, an
   interval of 330–400 days, and no fact in the same accession that assigns a
   different FY to that exact interval and semantic metric. The earliest
   `filed_date` original presentation is the identity anchor.
4. If candidates at that earliest rank agree canonically on FY and interval,
   they may collapse logically while all physical lineage remains. If they
   disagree, the identity is ambiguous; accession or raw ID is not a tiebreaker.
5. A comparative fact repeated in a later filing inherits the demonstrated FY
   of the original presentation for the exact interval. The later filing's
   `fy` cannot reassign the older period. A different FY claim is a structural
   conflict unless separately versioned correction evidence explicitly changes
   fiscal identity.
6. A fiscal-calendar pattern derived only from PIT-visible original
   presentations may corroborate an anchor and detect contradictions. It may
   not create an FY when no original anchor exists.
7. Otherwise the result is `UNRESOLVED` and `INELIGIBLE`.

Every piece used by this resolution receives lineage and participates in the
PIT availability watermark defined in §11.

For repeated comparative facts, the fiscal identity comes from the earliest
valid original presentation of that period. A later filing cannot reassign an
older period to the later filing's FY metadata. The selected value may still
come from a later visible filing or amendment.

`canonical_period_end` is the demonstrated fiscal-year end. A company whose
fiscal year closes in September, June, or on a 52/53-week calendar is not
realigned to 31 December.

### 8.4 52/53-week calendars and transition periods

Intervals of 330–400 days may pass the ordinary annual-duration structural
check. A 52-week or 53-week year remains annual when the full identity contract
is satisfied.

A transition or stub period is `OTHER_DURATION`, even if filed on a `10-K` or
`10-K/A`. It is auditable but ineligible for A v1. Supporting transition CAGR
would require a new normalizer/policy version and human approval.

### 8.5 Tag selection

Tag selection is metric-specific and versioned by the catalog
`sec-annual-tags-v1`. Its initial ordered candidates are:

```text
EPS_DILUTED: EarningsPerShareDiluted
REVENUE: RevenueFromContractWithCustomerExcludingAssessedTax,
         Revenues, SalesRevenueNet, SalesRevenueGoodsNet
NET_INCOME: NetIncomeLoss, ProfitLoss
DILUTED_SHARES: WeightedAverageNumberOfDilutedSharesOutstanding,
                WeightedAverageNumberOfShareOutstandingBasicAndDiluted
```

The catalog is evaluated only over evidence visible at the requested `as_of`:

1. consider only tags whose semantic class matches the requested metric;
2. rank by distinct demonstrated fiscal-year coverage;
3. break equal coverage using the documented candidate order;
4. retain all candidate observations in raw and normalized evidence;
5. mark unknown or semantically uncertain tags for review;
6. never relabel Basic EPS as Diluted EPS.

For A v1 automatic EPS calculations, the selected metric must be
`EPS_DILUTED`. `EPS_BASIC` may be preserved as its own metric but cannot satisfy
the diluted contract. `EPS_UNSPECIFIED` is ineligible.

### 8.6 Filings, amendments, and restatements

At a requested `as_of`, resolve a company/metric/fiscal-year only after PIT
filtering.

Within visible eligible SEC evidence:

1. use the annual tag selected by the versioned tag policy;
2. group by demonstrated fiscal identity;
3. select the latest visible filing by `filed_date`;
4. use stable raw identity only to order semantically identical duplicates;
5. never use raw ID to choose between contradictory values.

A visible `10-K/A` can supersede a `10-K` only when an approved, lineage-bearing
SEC filing-metadata source explicitly and uniquely links the amendment
accession to that original accession. Matching company, FY, dates, form family,
or values is not proof of that relationship. Until such relationship evidence
is persisted and visible, the amendment is retained for audit but cannot
supersede automatically.

After that relationship is demonstrated, company, metric, fiscal identity,
exact period start/end, unit, currency, scale, and semantic tag class must also
match. The amendment may change the reported value, but it may not silently
change fiscal identity. Any incompatibility is a structural conflict and fails
closed.

A later restatement creates a new normalized observation. Before its
`identity_available_at`, historical queries retain the earlier visible value;
at and after its visibility, the resolver may select it. Superseded filings
remain auditable.

If candidates at the maximum filing rank differ canonically in value, unit,
period, fiscal identity, scale, currency, or semantic tag, they are
structurally contradictory regardless of numeric magnitude. The result is
`REVIEW_REQUIRED_HIGH` and no automatic effective value is produced; raw ID is
never a tiebreaker. Only candidates identical under the versioned semantic
canonicalization may be collapsed logically, and all physical lineage remains.

The following cases fail closed: two annual intervals partially overlap while
claiming one fiscal year; one accession presents incompatible durations for
the same metric and fiscal identity; a duplicate has a different semantic
payload; or an amendment cannot prove compatibility with its original. A Q4
quarterly fact and an FY annual fact sharing an end date remain distinct
because their period types and starts differ.

A later `10-K` that repeats an older exact interval is a restatement candidate,
not a new identity anchor. It uses the original FY and may become the selected
value only after it is visible, uniquely latest by `filed_date`, and compatible
on every non-value semantic field. Equal latest rank plus incompatible
semantics is ambiguity and produces no automatic value. A single accession
with incompatible durations for one metric/FY is always a structural conflict.

## 9. Future Yahoo annual normalization

The future annual Yahoo normalizer is conceptually named:

```text
yahoo-annual-normalized-v1
```

It is not part of the first SEC implementation. Annual Yahoo datasets require
new explicit variants; quarterly variant names must not be reused. Candidate
names are:

- `yahoo.annual_fundamentals_timeseries`;
- `yfinance.annual_income_stmt`.

The exact provider alias that produced a value is retained. Basic, Diluted,
and Unspecified EPS remain separate. Yahoo receives FY identity only from
demonstrable metadata or an unambiguous fiscal calendar. Proximity to a SEC
period is corroborating evidence only.

## 10. Effective policy `a-annual-v1`

Annual selection uses a separate policy:

```text
a-annual-v1
```

It accepts only observations that satisfy all of:

- `period_type='ANNUAL'`;
- `observation_kind='REPORTED'`;
- approved annual normalizer version and source variant;
- demonstrated `fiscal_year`;
- `fiscal_quarter IS NULL`;
- demonstrated `canonical_period_end`;
- compatible metric, tag, unit, currency, and scale;
- `selection_eligibility='ELIGIBLE'`;
- visibility at the requested `as_of`.

The policy never calls, wraps, or aliases `c-v2.6-compatible-v1`.

Annual resolution is by fiscal identity, not by a ±35-day date match. Date
distance can support a comparison, but it cannot establish FY identity.

## 11. Point-in-time contract

The mandatory order is:

```text
limit every fact and every supporting evidence item to observed_at <= as_of
  -> resolve identity and period policy
  -> compute identity_available_at
  -> retain only evidence/calculations with identity_available_at <= as_of
  -> filing/amendment resolution
  -> source selection
  -> growth calculations
  -> CAGR calculations
  -> future score
```

Filtering the current winner after selection is prohibited.

`identity_available_at` is the maximum `observed_at` of every evidence item
needed to demonstrate identity or eligibility, including the raw fact, the
original filing, an amendment, fiscal-calendar evidence, tag-coverage evidence,
and any other supporting observation. The future annual resolution lineage must
retain the supporting raw/observation IDs and this watermark. An annual
resolution or derived calculation is not usable for an `as_of` earlier than its
watermark. If required supporting evidence is missing or its availability
cannot be demonstrated, the result is `REVIEW_REQUIRED` or `INELIGIBLE`, never
an inferred historical identity.

`filed_date` describes and ranks SEC filings inside the already-visible set. It
does not replace `observed_at` and does not prove intraday public availability.

`source_available_at` is used only when a reliable public timestamp exists. A
future publicly-available mode requires its own specification and must not
infer a timestamp from a filing date.

Normalizing old raw evidence later retains the raw `fetched_at` as
`observed_at`; normalizer execution time does not rewrite that fact's
availability. It does not bypass `identity_available_at`: if later evidence
was needed to interpret the old fact, the later watermark governs eligibility.

An annual backfill first observed today cannot certify what this installation
knew before today, even when its filing date is historical.

### 11.1 Append-only resolution model

Base normalization and contextual resolution are distinct. The current
`UNIQUE(raw_id, normalizer_version)` remains suitable for a context-free base
normalization; it is not, by itself, the identity of a future fiscal/tag/filing
resolution that may depend on evidence arriving later.

Conceptually, each future resolution has this immutable identity:

```text
raw_id
normalizer_version
resolution_policy_version
identity_available_at
lineage_fingerprint
```

`lineage_fingerprint` is the versioned canonical digest of the ordered set of
supporting evidence identities, each containing evidence kind, raw or
observation ID, `observed_at`, and its semantic digest. A different supporting
set, watermark, or interpretation produces a new resolution record. It never
updates or deletes an earlier resolution, even when both concern the same raw
fact.

At `as_of=T`, the PIT selector first limits facts and supporting evidence to
`observed_at <= T`, then considers only resolutions whose
`identity_available_at <= T`. It chooses under the applicable versioned policy
from that visible set; a later resolution cannot alter an earlier query. Equal
rank with incompatible semantics fails closed. Changes in fiscal-calendar
evidence, tag coverage, original-filing evidence, amendment relationships, or
restatement evidence therefore create later resolutions rather than rewriting
history.

### 11.2 Physical-storage boundary

A1.1 freezes the conceptual contract above but does **not** implement its final
physical storage, auxiliary-lineage relation, re-resolution table, or annual
PIT selector. A1.1 must only avoid a schema choice that prevents a future
one-to-many relation from base normalized evidence to immutable resolutions.

Before A1.2 persists an annual interpretation, it must define and test the
versioned physical resolution/lineage model. Before A1.3 selects any annual
value, that model must support the identity, watermark, multiple resolutions,
late tag-coverage reranking, and PIT selection rules above. Neither stage may
overload `UNIQUE(raw_id, normalizer_version)` as the resolution identity.

This deferral does not block A1.1 because A1.1 stores no annual evidence and
performs no annual identity, tag, filing, or PIT resolution. Its temporal and
raw-identity changes are additive and preserve the keys needed by the later
one-to-many extension.

## 12. Source priority and isolation

SEC is the primary source for US issuers when eligible annual SEC evidence
exists.

Yahoo may be used for:

- explicit comparison with a fiscal-year-compatible SEC value;
- explicit fallback when no eligible SEC value exists and Yahoo FY identity is
  independently demonstrated;
- complementary diagnostics.

Yahoo never silently displaces SEC. A numeric discrepancy alone does not
change SEC priority.

The effective layer may expose source-specific candidate series, but the
primary `annual_eps_series` used for growth and CAGR is single-source. If SEC
does not provide a continuous interval, Yahoo cannot fill isolated SEC gaps to
manufacture a CAGR. A complete Yahoo sequence may be exposed as a separately
identified fallback sequence.

Every YoY transition and CAGR records one source, its observation IDs, raw IDs,
and fiscal years. Cross-source arithmetic is prohibited.

For contextual SEC/Yahoo diagnostics, A v1 may reuse the existing percentage
bands under a new annual comparison-rules version:

- `<= 0.1%`: minor difference;
- `> 0.1% and < 1%`: discrepancy recorded;
- `>= 1% and <= 5%`: review required;
- `> 5%`: high review required.

These bands diagnose evidence; they do not define the A score.

## 13. Pre-score A contract

The pre-score contract is versioned independently from the selection policy.
It contains data and diagnostics, not points or weights.

The first contract identifier is `a-pre-score-v1`. Every response includes
exactly named top-level fields `contract_version`, `contract_status`,
`contract_usability`, `reasons`, `company_id`, and `as_of`, plus the result
fields in §13.1. The names `status` and `usability` are not aliases.
`contract_version` is the literal `a-pre-score-v1`; `company_id` is an integer;
`reasons` is an ordered list of stable reason-code strings; and `as_of` is a
timezone-aware UTC RFC 3339 timestamp. Dates serialize as `YYYY-MM-DD`, exact
financial decimals and percentages as canonical base-10 strings, counts as
JSON integers, booleans as JSON booleans, and absent values as JSON null.

`contract_status` has this closed domain:

- `OK`: every mandatory result is available and no review condition exists;
- `PARTIAL`: mandatory core results are valid, while an explicitly optional
  result or diagnostic is unavailable;
- `REVIEW_REQUIRED`: evidence exists but a contextual condition requires human
  review before automatic use;
- `REVIEW_REQUIRED_HIGH`: a severe contradiction or integrity risk exists;
- `INSUFFICIENT_DATA`: mandatory history or evidence is absent;
- `ERROR`: acquisition-independent contract construction failed an invariant.

`contract_usability` has this closed domain:

- `USABLE`: permitted only with `contract_status='OK'`;
- `USABLE_WITH_WARNINGS`: permitted only with `contract_status='PARTIAL'` and
  only when every mandatory result consumed is individually `OK`;
- `NOT_USABLE`: mandatory for `REVIEW_REQUIRED`, `REVIEW_REQUIRED_HIGH`,
  `INSUFFICIENT_DATA`, and `ERROR`.

In particular, `REVIEW_REQUIRED_HIGH` always implies
`contract_usability='NOT_USABLE'`.

### 13.1 Required minimum fields

- `annual_eps_series`: ordered, single-source observations with FY, period,
  value, source, selected observation ID, raw ID, filing identity, and PIT
  timestamps;
- `latest_annual_eps`;
- `previous_annual_eps`;
- `latest_annual_eps_yoy_pct`;
- `annual_eps_yoy_history`;
- `eps_cagr_3y`;
- `positive_growth_years`;
- `negative_growth_years`;
- `loss_to_profit`;
- `usable_fiscal_year_count`;
- `missing_fiscal_years`;
- `annual_data_integrity`;
- `annual_source_integrity`;
- `selection_policy_version`, fixed to `a-annual-v1`.

Every calculated data field from `annual_eps_series` through
`missing_fiscal_years` is an object with exactly this common envelope plus its
typed `value`. The two integrity fields instead use their exact diagnostic
shapes below:

```text
value
calculation_status
reasons
provenance = {
  source, source_variant, normalizer_versions, selection_policy_version,
  resolution_policy_version, observation_ids, raw_ids, fiscal_years,
  as_of, identity_available_at, lineage_fingerprint
}
```

The series entries carry the same provenance. A value without this lineage is
not a valid A contract result.

`calculation_status` has this closed domain:

- `OK`: the value was calculated under the complete contract;
- `NOT_AVAILABLE`: no eligible observation exists;
- `INSUFFICIENT_HISTORY`: fewer than the required continuous years;
- `NOT_APPLICABLE`: the calculation does not apply to that evidence;
- `REVIEW_REQUIRED`: evidence exists but needs contextual review;
- `REVIEW_REQUIRED_HIGH`: evidence has a severe structural or integrity
  conflict and no automatic value may be emitted;
- `INVALID_FOR_CALCULATION`: the mathematical contract is not satisfied.

Only `OK` permits a non-null numeric calculated value. Counts and lists that
are validly empty use `OK` with their empty typed value; absence is not encoded
as an empty result. Every non-`OK` calculation has `value=NULL`, except a
diagnostic boolean/list explicitly defined as raw evidence rather than a
calculation.

These calculation statuses are distinct from observation quality,
selection eligibility, aggregate integrity, and contract usability.

### 13.2 Calculation semantics

`annual_eps_series` is sorted by fiscal identity, not natural-year date.

An annual YoY percentage requires consecutive fiscal years from the same
source and a strictly positive prior EPS. If prior EPS is zero or negative,
the percentage is `NULL` with an explicit reason. Raw direction and
loss-to-profit remain available.

`eps_cagr_3y` is anchored at the most recent effective fiscal year visible at
`as_of`. If that year is `FY=N`, the only permitted three-year window is
`N-3, N-2, N-1, N`, all from one source and all individually visible and
eligible. It requires strictly positive first and last EPS. An older complete
four-year block must not be substituted when any member of the latest anchored
window is missing. Fifth and sixth historical years never change the anchored
window:

```text
((latest_eps / earliest_eps) ** (1 / 3) - 1) * 100
```

If any anchored year is missing, CAGR is `NULL` with
`INSUFFICIENT_HISTORY`. If source identity or evidence integrity fails, it is
`NULL` with `REVIEW_REQUIRED` or `REVIEW_REQUIRED_HIGH` as appropriate. If
either required endpoint is zero or negative, CAGR is always `NULL` with
`INVALID_FOR_CALCULATION`; no numeric CAGR is emitted merely with a review
flag. Loss-to-profit and other sign diagnostics remain available separately.
An intermediate non-positive EPS keeps the interval non-CAGR-usable and is
reported explicitly; it never authorizes cross-source arithmetic or a silent
gap skip.

`eps_cagr_5y` is optional and requires six continuous fiscal years under the
same rules.

`positive_growth_years` counts consecutive transitions where current EPS is
greater than prior EPS. `negative_growth_years` counts transitions where it is
lower. Flat transitions are reported separately in diagnostics and belong to
neither count.

`loss_to_profit` describes the latest consecutive transition where prior EPS
is negative or zero and current EPS is positive.

`usable_fiscal_year_count` counts effective observations in the selected
single-source primary sequence. `missing_fiscal_years` lists gaps between the
oldest and latest FY in that sequence.

`annual_data_integrity` and `annual_source_integrity` are structured objects
and are not borrowed from C. `annual_data_integrity` has this exact shape:

```text
{
  status: COMPLETE | PARTIAL | REVIEW_REQUIRED |
          REVIEW_REQUIRED_HIGH | INSUFFICIENT_DATA,
  reasons: [stable_reason_code...],
  observation_ids: [int...],
  raw_ids: [int...],
  missing_fiscal_years: [int...],
  conflicting_evidence_groups: [{kind, evidence_ids, reason}...],
  latest_identity_available_at: datetime | null
}
```

It describes evidence completeness and structural consistency, not whether one
formula succeeded. `annual_source_integrity` has this exact shape:

```text
[
  {
    source,
    source_variant,
    status: MATCHED | NOT_COMPARED | MINOR_DIFFERENCE |
            DISCREPANCY_RECORDED | REVIEW_REQUIRED |
            REVIEW_REQUIRED_HIGH | STRUCTURAL_MISMATCH,
    reasons: [stable_reason_code...],
    observation_ids: [int...],
    raw_ids: [int...],
    fiscal_years: [int...],
    comparison_rules_version,
    comparison_pairs: [
      {left_observation_id, right_observation_id, difference_pct, status}...
    ]
  }...
]
```

Calculation status, evidence integrity, source comparison, and contract
usability remain separate namespaces.

### 13.3 Optional A v1 extensions

The following do not belong to the minimum contract unless separately
approved:

- annual revenue and revenue YoY/CAGR;
- annual net income and net-income YoY/CAGR;
- net margin and margin trend;
- annual acceleration/deceleration;
- ROE;
- SEC/Yahoo comparison fields beyond source-integrity diagnostics.

ROE requires an approved equity metric, instant-period semantics, and a
defined average-equity convention. It must not be inferred from the current
metric catalog.

## 14. Quality, eligibility, and fail-closed behavior

The existing separation remains:

- intrinsic observation quality;
- selection eligibility;
- contextual source comparison;
- calculation status;
- annual data integrity;
- contract status and usability.

These namespaces are not interchangeable:

- `intrinsic_quality_status` describes the observation itself;
- `selection_eligibility` says whether a policy may select it;
- `calculation_status` describes one derived value;
- `annual_data_integrity` aggregates history and evidence completeness;
- `contract_status` describes the A response as a whole;
- `contract_usability` describes whether a consumer may use it automatically.

The closed aggregate domains and their usability mapping are defined only in
§13. `INELIGIBLE` belongs to `selection_eligibility`; it is not a
`contract_status`. `annual_data_integrity` uses its own closed domain from
§13.2, and `calculation_status` describes only one result envelope.

### 14.1 Required classifications

| Situation | Required outcome |
|---|---|
| FY missing or ambiguous | Observation `selection_eligibility='INELIGIBLE'`; aggregate at least `contract_status='REVIEW_REQUIRED_HIGH'` and `contract_usability='NOT_USABLE'`. |
| Period demonstrably non-annual | `OTHER_DURATION`; `selection_eligibility='INELIGIBLE'` for A. |
| Temporal type unresolved | `UNRESOLVED`; `selection_eligibility='INELIGIBLE'`. |
| Basic EPS only | Preserve `EPS_BASIC`; `selection_eligibility='INELIGIBLE'` for diluted-EPS calculations. |
| EPS semantic alias unknown | Preserve as `EPS_UNSPECIFIED`; `selection_eligibility='INELIGIBLE'`. |
| Candidate tags tie with canonically identical evidence | Deterministic candidate order may resolve; record diagnostic. |
| Candidate tags conflict semantically or numerically at maximum rank | No effective observation; `REVIEW_REQUIRED_HIGH`. |
| Latest filing candidates contradict | No raw-ID tiebreak; `REVIEW_REQUIRED_HIGH`. |
| 10-K/A explicitly linked, canonically compatible, and uniquely later | Select only after visible; original remains audit evidence. |
| Amendment identity contradicts original | `REVIEW_REQUIRED_HIGH`; no silent supersession. |
| Later restatement is unique and matches all required non-value semantic fields | Select after visibility; earlier PIT states unchanged. |
| Missing internal fiscal year | Existing rows remain; aggregate `contract_status='INSUFFICIENT_DATA'`; calculations crossing the gap have `calculation_status='INSUFFICIENT_HISTORY'` and are not usable. |
| Fewer than four anchored continuous years | `eps_cagr_3y.value=NULL`; `calculation_status='INSUFFICIENT_HISTORY'`; aggregate `contract_status='INSUFFICIENT_DATA'` when CAGR is mandatory. |
| SEC/Yahoo structural mismatch | Do not compare or substitute; at least `REVIEW_REQUIRED`. |
| SEC/Yahoo numeric difference 1–5% inclusive | Contextual `REVIEW_REQUIRED`; SEC remains primary. |
| SEC/Yahoo numeric difference above 5% | Contextual `REVIEW_REQUIRED_HIGH`; SEC remains selected but automatic A use is withheld pending policy. |
| Yahoo fallback with proven identity | Explicit fallback and provenance; never reported as SEC. |
| Yahoo fallback identity ambiguous | `selection_eligibility='INELIGIBLE'`. |

Negative EPS is valid evidence and is not rejected. It affects availability of
percentage/CAGR calculations and supports loss-to-profit diagnostics.

## 15. Isolation of C

The future implementation must preserve all of the following unchanged:

- `fundamental_c.py`;
- `c_score_v1.py`;
- `database/c_fundamentals_adapter.py`;
- the 11-input C contract;
- `c-v2.6-compatible-v1`;
- the effective C view and its 37-column contract;
- quarterly source priority and selection;
- corporate-action persistence and acquisition;
- split-integrity reconstruction.

The exact 11-input C contract protected by this specification is:

1. `latest_eps_yoy_pct`;
2. `previous_eps_yoy_pct`;
3. `eps_acceleration_pp`;
4. `latest_revenue_yoy_pct`;
5. `previous_revenue_yoy_pct`;
6. `revenue_acceleration_pp`;
7. `latest_eps`;
8. `eps_yoy_pct`;
9. `eps_loss_to_profit`;
10. `data_integrity`;
11. `split_integrity_status`.

Annual evidence cannot enter C because:

1. annual observations use annual normalizer versions not accepted by C;
2. annual observations have `period_type='ANNUAL'`;
3. annual observations have `fiscal_quarter=NULL`;
4. the unchanged C view requires its pinned quarterly versions and a non-null
   fiscal quarter;
5. A uses `a-annual-v1`, never the C policy.

The addition of `period_type` must not require recreating or altering the C
view. The new column is not part of the view's output contract.

Future gates must prove the repository-backed C baselines:

- `sec_effective=75`;
- `hybrid_effective=60`;
- exact equality of all 11 C inputs and their established score contract.

No annual implementation gate may substitute an unproven effective-row count
for these baselines.

## 16. Invariants

1. Raw evidence is never overwritten by normalization or selection.
2. Each normalized observation represents one raw fact, metric, source,
   temporal type, and normalizer version.
3. Q4 and FY facts with the same end date, filing, tag, and accession can
   coexist because their starts differ.
4. Annual evidence is never selected as quarterly.
5. Quarterly evidence is never selected as annual.
6. A Q4 fact in a 10-K remains quarterly when its interval is quarterly.
7. FY identity is not determined by natural year.
8. FY identity is not determined by duration alone.
9. `fiscal_quarter` is null for annual observations.
10. Basic EPS is never silently transformed into Diluted EPS.
11. Unspecified EPS is never used as Diluted EPS.
12. Amendments affect only PIT states in which they are visible.
13. Restatements do not rewrite earlier PIT results.
14. Superseded filings remain auditable.
15. Raw ID never substitutes for SEC accession identity.
16. A conflicting same-rank filing is not resolved by raw ID.
17. Sources are never mixed silently.
18. Every YoY and CAGR remains within one source.
19. Yahoo never silently displaces eligible SEC evidence.
20. Gaps are reported and never bridged by cross-source arithmetic.
21. Ambiguous temporal, fiscal, filing, tag, or source identity fails closed.
22. A new interpretation requires a new normalizer or policy version.
23. No A change modifies C formulas, inputs, selection, corporate actions, or
    split integrity.

## 17. Required A1 fixtures

Synthetic fixtures shall cover at least:

1. ordinary 10-K annual fact with FY identity;
2. compatible 10-K/A visible after the original filing;
3. Q4 and FY facts with the same `period_end`, tag, filing date, and accession
   but distinct `period_start` and value;
4. 52-week fiscal year;
5. 53-week fiscal year;
6. short or long transition period classified as `OTHER_DURATION`;
7. approved alternate tag with lower and higher coverage cases;
8. candidate-order tie with compatible evidence;
9. conflicting tags at maximum rank;
10. contradictory filings with the same latest filing rank;
11. compatible later restatement and before/after-`as_of` behavior;
12. internal fiscal-year gap;
13. negative EPS;
14. zero/negative EPS to positive EPS;
15. Basic versus Diluted EPS evidence;
16. SEC/Yahoo matching evidence;
17. SEC/Yahoo 1%, 5%, and above-5% boundaries;
18. Yahoo fallback with demonstrated FY;
19. Yahoo fallback with ambiguous FY;
20. attempted cross-source CAGR, which must be rejected;
21. supporting identity evidence observed after the fact;
22. partially overlapping annual intervals claiming one FY;
23. one accession with two incompatible durations;
24. duplicate exact evidence and duplicate semantic conflict;
25. annual evidence without `period_start`;
26. identical raw retry and conflicting raw retry;
27. a quarterly writer during the `period_type` rollout;
28. a partial migration rerun and a divergent catalog definition;
29. rollback blocked by an existing Q4/FY coexistence pair;
30. two concurrent canonically identical raw inserts resolving to one ID;
31. two concurrent same-key raw inserts with incompatible semantics, producing
    `RAW_IDENTITY_CONFLICT` for the loser;
32. payloads with different JSON/key/number/timezone representations that are
    canonically equivalent, and a genuinely different payload that conflicts;
33. an old writer detected during the nullable rollout, which blocks progress;
34. new dual-schema application against the exact old schema;
35. old application against the new nullable schema, permitted only before the
    Phase 2 capability gate;
36. late auxiliary evidence creating a new immutable PIT resolution while the
    earlier resolution remains queryable;
37. tied incompatible original SEC presentations;
38. a `10-K/A` without a demonstrable relationship to an original `10-K`;
39. a gap immediately before the latest effective FY, proving that CAGR does
    not select an older complete window;
40. every row of the normative `period_type` consistency matrix, including
    Yahoo quarterly evidence without `source_period_start`.

Fixtures must retain raw identifiers, dates, forms, tags, units, values,
observed timestamps, and expected quality/eligibility decisions. Tests must be
fully offline until a later explicitly authorized PostgreSQL integration gate.

## 18. A0 acceptance criteria

A0 is approvable only when human review confirms:

- temporal semantics are explicit and exhaustive for the initial domain;
- `period_type` rollout cannot classify NULL implicitly and cannot break the
  current writers;
- annual SEC identity uses combined fiscal evidence rather than one heuristic;
- point-in-time ordering is unambiguous;
- supporting evidence has the conceptual immutable lineage and
  `identity_available_at` contract in §11, with physical storage explicitly
  gated before A1.2/A1.3;
- Q4 and FY can coexist without raw collision;
- raw retries are idempotent and semantic conflicts fail closed;
- concurrent raw retries converge through the unique index without global
  locks, and canonical equality is representation-independent;
- the PostgreSQL 17 index replacement and rollback protocol is explicit;
- amendments and restatements preserve historical visibility;
- source priority and fallback are explicit;
- cross-source YoY/CAGR is prohibited;
- the minimum A contract is separate from any future score;
- the A contract has versioned provenance, null reasons, the exact
  `contract_status`, and the exact `contract_usability` domains;
- optional extensions are not silently required by A v1;
- ambiguous states fail closed;
- C isolation uses repository-proven baselines only;
- no implementation or deployment is authorized by the spec itself.

## 19. Deferred human decisions

These decisions are deliberately deferred and must be resolved before the
stage that consumes them:

1. Whether A v1 scoring remains EPS-only or includes revenue/net income
   confirmation. Recommendation: EPS-only core; revenue and net income remain
   diagnostics initially.
2. Minimum history required for an A score beyond the fixed four-year
   requirement for calculating three-year CAGR.
3. Score thresholds, weights, classes, and treatment of missing components.
4. Whether and when ROE and margin metrics enter A. ROE requires an equity
   evidence contract first.
5. Whether annual Yahoo ingestion is enabled after SEC validation.
   Recommendation: defer it until the SEC annual path is proven.
6. Whether a future public-availability PIT mode is needed in addition to the
   current observed-availability mode.
7. Whether transition fiscal periods receive a future adjusted comparison
   policy. A v1 excludes them.

These product and scoring decisions do not block A1.1. The following technical
decisions are frozen for A1.1:

1. A1.1 may modify `database/fundamentals.py`,
   `database/normalized_fundamentals.py`,
   `database/backfill_semantics_v2.py`, the minimum affected normalized
   loaders, fixtures and tests, and the new versioned rollout/migration
   artifacts. It must not modify `fundamental_c.py`, `c_score_v1.py`,
   `database/c_fundamentals_adapter.py`, C financial behavior, corporate
   actions, or split integrity.
2. The nullable-to-explicit-write-to-validated-`NOT NULL` rollout in §6 is
   mandatory; no permanent quarterly default is permitted.
3. The `raw-identity-v1` semantic comparison and `RAW_IDENTITY_CONFLICT`
   behavior in §7 are mandatory.
4. The PostgreSQL lock, timeout, index-swap, rerun, and rollback protocol in
   §6.1 is mandatory.
5. A1.1 freezes but does not physically implement the future resolution
   identity in §11.1. Annual normalization cannot persist interpretations until
   A1.2 supplies the versioned physical lineage/resolution model, and A1.3
   cannot select annual evidence until PIT behavior is proven.

These are implementation-safety decisions, not A score decisions.

## 20. Subsequent work

### A1.1 — Temporal infrastructure

- add `period_type` through a new versioned migration, initially nullable;
- update the shared Python model and all quarterly writers to emit
  `QUARTERLY` explicitly before imposing `NOT NULL`;
- classify the current AAPL 330-row manifest as quarterly under fail-closed
  gates, while validating the real population on every deployment;
- replace raw uniqueness with the period-start-aware, NULL-safe identity;
- implement complete lookup, retry, semantic equality, and
  `RAW_IDENTITY_CONFLICT` behavior;
- implement and test the concurrent raw-write contract without global locks;
- update the minimum affected loaders, materializing fixtures, and tests so
  persisted observations always carry explicit `period_type`;
- add schema, idempotency, coexistence, rollout, rollback, and C-regression
  tests;
- preserve extension points for the future one-to-many resolution model, but
  do not create its physical storage;
- do not deploy until separately approved.

### A1.2 — SEC annual extraction and normalization

- extract annual facts without changing the quarterly extractor;
- persist Q4 and FY independently;
- define and implement the physical append-only lineage/resolution model from
  §11 before persisting an annual interpretation;
- implement `sec-annual-normalized-v1`;
- resolve FY, tags, filings, amendments, and restatements;
- prove offline fixtures before controlled PostgreSQL work.

### A1.3 — Effective A selection and PIT

- implement `a-annual-v1`;
- select among visible immutable resolutions using `identity_available_at` and
  lineage fingerprints;
- construct source-isolated annual series;
- implement observed-as-of reconstruction;
- expose the pre-score A contract without scoring.

### A1.4 — Optional annual Yahoo evidence

- add explicit annual source variants;
- implement `yahoo-annual-normalized-v1`;
- validate fiscal identity, comparison, and explicit fallback;
- retain source-isolated calculations.

### A2 — Final contract and score

- resolve human decisions about scope, minimum history, weights, and
  thresholds;
- freeze score fixtures and acceptance criteria;
- implement scoring only after approval.

### A3 — Integration and release

- controlled PostgreSQL deployment;
- multi-company validation including non-calendar and 53-week fiscal years;
- C regression using `75`, `60`, and the exact 11-input contract;
- independent release review;
- no push without explicit authorization.

## 21. First implementation task after approval

The first bounded implementation task shall create, but not deploy, the A1.1
migration, shared-model/write-path changes, and tests for:

- the `period_type` column and domain;
- explicit quarterly writer output and deterministic classification of the
  current AAPL 330-row manifest;
- replacement of raw uniqueness to include `period_start` with the §7 NULL
  semantics;
- complete raw lookup/retry/conflict handling;
- canonical `raw-semantic-canonical-v1` comparison and concurrent identical /
  incompatible insert behavior;
- coexistence of same-ending Q4 and FY raw evidence;
- affected loaders and fixtures that materialize `NormalizedObservation`;
- the phased PostgreSQL 17 application/schema protocol, capability gate,
  rerun, forward-recovery, and rollback guards;
- preservation of the C view contract and repository-proven C baselines.

It shall not acquire annual evidence, normalize annual rows, create an A
effective policy, implement physical annual lineage/resolution storage, modify
C, apply PostgreSQL DDL, stage, commit, or push unless separately authorized.
