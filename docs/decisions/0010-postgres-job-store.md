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
  without the work ever completing — acceptable only because PR-2-03 adds lease
  expiry recovery, and a stuck job is visible as `RUNNING` with an expired lease
  rather than silently vanishing.

## References

- [API Engineering Syllabus §8](../API_ENGINEERING_SYLLABUS.md)
- [ADR 0008](0008-execution-boundary.md) (PostgreSQL 16 boundary)
