# ADR 0010: PostgreSQL-backed job store (syllabus ADR-005)

## Status

Proposed

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

## References

- [API Engineering Syllabus §8](../API_ENGINEERING_SYLLABUS.md)
- [ADR 0008](0008-execution-boundary.md) (PostgreSQL 16 boundary)
