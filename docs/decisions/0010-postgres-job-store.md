# ADR 0010: PostgreSQL-backed job store (syllabus ADR-005)

## Status

Accepted

Accepting because PR-2-02 lands the code that implements this decision: the
`0003_job_leases` migration, the `FOR UPDATE SKIP LOCKED` claim in
`02-async-job-api/app/repository.py`, and the separate worker process in
`app/worker.py`. Owner authorization was given for this transition.

## Context

Project 2 needs durable job state, worker claims, and client status queries. Introducing Redis or a message broker before demonstrating need would violate the syllabus “smallest viable system” principle.

## Decision

1. Store job records in **PostgreSQL** in the `02-async-job-api/` database, separate from Project 1 schema.
2. Use **row-level locking** (`FOR UPDATE SKIP LOCKED` or equivalent) for worker claims.
3. Run the **worker as a separate process** from the HTTP API; both share `DATABASE_URL`.
4. Defer external brokers until a later milestone proves they are required.

## Consequences

### Positive

- One operational datastore for local dev and CI.
- Transactions align claim and status updates.

### Negative

- Polling and claim traffic load PostgreSQL; acceptable at lab scale.
- Not a substitute for high-throughput queue products.
- **`attempt` is consumed at claim time, not at completion.** The claim statement
  increments `attempt` in the same transaction that sets `RUNNING`, so a worker
  that crashes mid-job has already spent an attempt even though it produced no
  result. This is deliberate: it makes a crash-looping job terminate instead of
  retrying forever, and it keeps the attempt counter honest about how many times
  work was actually started. The cost is that a crash can exhaust a job's attempts
  without the work ever completing — acceptable because PR-2-03 adds lease expiry
  recovery, and a stuck job is visible as `RUNNING` with an expired lease rather
  than silently vanishing.
- **The claim admits expired leases, and `ix_jobs_lease_expiry` is load-bearing.**
  Since PR-2-03 the claim runs two probes: one for `RUNNING` rows whose lease has
  expired, one for due `PENDING` rows. The partial index
  `(lease_expires_at) WHERE status = 'RUNNING'` is what keeps the reclaim probe an
  index scan rather than a table scan — it is a correctness-of-throughput
  dependency, not observability. The two probes are separate statements on purpose:
  a single predicate OR-ing both statuses defeats both partial indexes and falls
  back to a sequential scan with a disk sort.
- **Recovery is worker-driven, not a scheduler.** The sweep that terminally fails
  exhausted expired jobs runs inline at the start of every worker cycle. If no
  worker is running, nothing recovers.
- **The lease is renewable, and renewal is owner-scoped.** Since PR-2-06 a worker
  renews the lease of the job it is running every `LEASE_SECONDS / 3`, so
  `LEASE_SECONDS` bounds how long a worker may go silent rather than how long a
  handler may run. `RENEW_LEASE_SQL` repeats the `lease_owner = :worker_id` guard
  that `COMPLETE_SQL` and `FAIL_SQL` carry, so renewal is the third write that cannot
  cross an ownership change: a worker whose lease was taken over renews zero rows
  and learns it has lost the job *before* it tries to complete. That is what makes a
  lost lease detectable rather than silently overwritten, and it is why a renewal is
  never a reclaim — zero rows ends the renewal, it does not take the job back.
  `attempt` is untouched by renewal; only claim and reclaim consume budget.
- **A live job is not reconcilable.** The effect reconciler's age filter is only a
  candidate list; the decision is taken under the effect row lock and skips any
  `INTENT` row whose job still holds a live lease. Age alone cannot make that call
  now that a lease can be renewed indefinitely. The residual gap is a handler that
  loses its lease and *then* applies, which no lease check can catch.

## References

- [API Engineering Syllabus §8](../API_ENGINEERING_SYLLABUS.md)
- [ADR 0008](0008-execution-boundary.md) (PostgreSQL 16 boundary)
