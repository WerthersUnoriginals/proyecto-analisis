# Persisted corporate-action capture design

## Scope and invariants

This contract persists provider evidence needed to replay the existing split-integrity logic. It does not persist `split_integrity_status` or `data_integrity`, change C formulas, change fundamental selection, call a provider, or grant database permissions.

## Relational model

`corporate_action_captures` is one immutable acquisition attempt. `corporate_action_events` contains its ordered zero-or-more stock-split observations. A successful complete capture with zero children means `NO_RECENT_SPLITS`; it is not equivalent to a failed or unknown attempt.

The complete column, key, constraint, and index contract is executable in `database/migrations/2026-09-23_corporate_action_capture_v1.sql`. Production-role grants are deliberately absent and require a later human decision.

## Point-in-time contract

For `(company_id, source_variant, capture_contract_version, as_of)`, select the latest capture whose `observed_at <= as_of`, ordered by `(observed_at DESC, id DESC)`. That attempt wins regardless of outcome. A later failed, partial, or unknown attempt produces `UNKNOWN`; consumers must not silently fall back to an earlier success. `source_available_at` is provider-declared availability when present; `observed_at` is the locally knowable time and equals completion time.

`legacy-split-capture-v1` requests a six-year inclusive lower bound and an open upper bound (`requested_window_end_at IS NULL`), matching the legacy yfinance read.

## Idempotency contract

The acquisition orchestrator deterministically derives a UUIDv5 `capture_uid` from the semantic request identity (company, ticker, provider, source variant, exact six-year window start, and `as_of`) before any provider call or database write. The UID is mandatory input to the repository; the repository never supplies a default. The same semantic capture/evidence therefore receives the same UID across retries, while a different request identity receives a different UID. Equal parameters do not silently collapse different evidence: reuse of the UID with different immutable capture fields or events produces `IDEMPOTENCY_CONFLICT` and writes nothing.

If a process fails before commit, the transaction leaves no bundle and a retry with the same UID inserts it. If commit succeeds but acknowledgement is lost, the orchestrator calls the public `load_capture_by_uid()` preflight and returns the immutable existing bundle without calling the provider again. Concurrent insertion uses the unique UID as the arbiter. Reuse of the UID with any different immutable capture field or event produces the sanitized `IDEMPOTENCY_CONFLICT` and writes nothing. A new acquisition request receives a new UID.

## Response fingerprint contract

Version: `corporate-action-response-sha256-v2`.

Included capture fields: fingerprint version, company ID, provider, source variant, capture contract version, requested window bounds, source availability, acquisition/completeness status, provider capture/revision IDs, provider metadata, error code, and sanitized error metadata. Included event fields: ordinal, action type, event date, split ratio, provider event/revision IDs, and provider metadata.

Excluded: capture UID, database IDs, created timestamps, capture start/completion/observation timestamps, and the fingerprint itself. These identify the local attempt, not the provider response.

Events are ordered by integer ordinal. Dates are `YYYY-MM-DD`; timestamps are UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`; NULL is JSON `null`; split ratios are positive arbitrary-precision PostgreSQL `NUMERIC` values serialized as base-10 strings without exponent or insignificant trailing zeros. Metadata objects have sorted string keys, arrays retain order, and JSON numbers are persisted in JSONB using the version-2 tagged representation `{"$type":"number","value":"<canonical-decimal>"}` so numeric values cannot collide with a literal metadata object and the stored evidence reproduces type and value without float loss. Serialization is UTF-8 JSON with sorted keys, ASCII escapes, no whitespace, and no NaN/Infinity. The digest is lowercase hexadecimal SHA-256. SUCCESS/PARTIAL evidence must carry non-null version and digest; FAILED/UNKNOWN has no response fingerprint because no complete provider response is claimed. Error metadata is allowlisted and `error_code` is a bounded uppercase symbolic code, never an exception message.

## SEC evidence resolver

The resolver reuses eligible `sec-normalized-v2` observations from `fundamentals_normalized`; it does not duplicate fundamentals. At `as_of`, it filters on `observed_at`, applies the six-year lower bound, chooses the tag with most distinct periods, preserves legacy candidate-order on coverage ties, and chooses the latest filed observation per period. Diluted-share tag quality uses the legacy classification. Amendments become visible only once their normalized evidence is observed. Unknown dynamically discovered tags are not declared fully equivalent unless their legacy candidate order is supplied/demonstrated.

Golden masters are mandatory for tag coverage, candidate-order ties, latest filing, diluted-share quality, and amendments before/after `as_of`. Full-contract equivalence remains unestablished if any of them fails.

## Derived status mapping

SUCCESS/COMPLETE plus zero events maps to `NO_RECENT_SPLITS`. A complete event set is evaluated with the unchanged legacy EPS/net-income/diluted-shares formula and aggregates to `VERIFIED_ALREADY_ADJUSTED`, `UNADJUSTED_DETECTED`, or `REVIEW_REQUIRED`. FAILED, UNKNOWN, PARTIAL, or non-complete evidence maps to `UNKNOWN`. These results are computed, never persisted as source evidence.
