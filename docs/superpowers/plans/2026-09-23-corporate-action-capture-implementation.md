# Corporate-action capture implementation plan

**Spec:** `docs/superpowers/specs/2026-09-23-corporate-action-capture-design.md`

1. Freeze migration, idempotency, fingerprint, point-in-time, and SEC golden-master expectations in offline tests; observe RED because implementation files are absent.
2. Add the two-table additive migration with no grants, revokes, legacy-table changes, or derived integrity columns.
3. Add pure capture/fingerprint/idempotency validation and point-in-time selection.
4. Add the legacy-compatible persisted SEC resolver and pure split-integrity replay.
5. Run focused tests, existing split/fundamental regression tests, full offline discovery, static migration inspection, `git diff --check`, and a final scope review. Do not stage, commit, push, call providers, or deploy the migration.
