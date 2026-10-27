# ADR 0011: Job submission idempotency (syllabus ADR-006)

## Status

Proposed

## Decision (preview for PR-2-04)

1. Clients may send **`Idempotency-Key`** (opaque string, max 255 chars) on `POST /v1/jobs`.
2. **Scope:** same key + same request body → same `job_id` and `202` on replay; same key + different body → `409 Conflict`.
3. **Retention:** keys are stored with the job row for the life of the job record (hard delete when job row is removed).
4. **Recovery guarantee:** at-most-once side effects are **not** claimed; workers must make external side effects idempotent or reconciliation must be explicit (syllabus falsification case 4).

## Consequences

Duplicate HTTP retries after a timeout can safely reuse the same logical job without duplicate work, within the retention window.

The explicit-reconciliation half of point 4 now exists: `0005_job_effects` adds a
transactional outbox of `(job_id, effect_key)` claims, and the worker reconciles
`INTENT` rows older than `EFFECT_RECONCILE_GRACE_SECONDS`. That makes a duplicate
side effect detectable and skippable after a reclaim, and it covers a crash between
the claim and the confirmation. It does not make a third-party call exactly-once:
the external operation must still be idempotent, normally by passing `effect_key` as
the vendor's idempotency key. The submission-key semantics in points 1–3 are still
to come in PR-2-04b.

## References

- [Project 2 plan](../plans/project-2-plan.md)
- [ADR 0010](0010-postgres-job-store.md)
