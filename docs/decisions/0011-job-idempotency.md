# ADR 0011: Job submission idempotency (syllabus ADR-006)

## Status

Accepted

Accepting because the code implementing this decision landed: the
`0006_job_idempotency` migration adds `jobs.idempotency_key` and
`jobs.request_fingerprint` behind a partial unique index,
`app/repository.py` inserts with `ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
DO NOTHING`, and `app/service.py` compares a canonical SHA-256 fingerprint before
answering `202`. The `Idempotency-Key` header, its 255-character limit and the
replay/conflict split are exercised in `tests/test_idempotency.py`, including an
eight-thread race for one key. Owner authorization was given for this transition.

## Decision

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
the vendor's idempotency key.

`Idempotency-Key` covers only the *submission* leg. A client that retries a `POST`
gets one job row, but a job that is reclaimed and re-run still re-enters its handler,
so it does not remove the need for the outbox — the two mechanisms answer different
questions and neither replaces the other.

### Scope of the key

The key is scoped to `POST /v1/jobs` alone. It is not a general-purpose request
namespace and it is not a client identity: two different clients that happen to send
`abc` address the same job, because the row stores the string and nothing else. Since
there is no authentication in this slice, a caller can confirm whether a key exists
by observing `409` instead of `202`. Do not send a secret as a key.

`max_attempts` is part of the fingerprinted request, and so is `type`. Changing either
under an existing key is a `409`, not a replay — the stored job would otherwise
silently keep the original value while the client believes its new request succeeded.

### Whitespace

The header is trimmed before validation and before hashing, so `" abc "` and `"abc"`
address the same job and one client cannot create two rows by padding. A header that
is empty or all whitespace is treated as **absent** rather than rejected: it carries
no identity, so falling back to the unkeyed path fails nothing a client could rely on
and avoids a `422` for a header the client cannot see.

### Retention

The key lives as long as the job row, with no separate expiry. There is no cleanup
job and no TTL, so a key is reusable only after the job is deleted. At lab scale that
is the smallest correct answer; a production system with per-tenant key namespaces
would need a retention policy and would not want to rely on row lifetime.

## References

- [Project 2 plan](../plans/project-2-plan.md)
- [ADR 0010](0010-postgres-job-store.md)
