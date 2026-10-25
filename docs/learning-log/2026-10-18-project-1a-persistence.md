# 2026-10-18 — Project 1A persistence (`01-rest-api`)

## What I did

- Merged saved-item CRUD with PostgreSQL, Alembic `0001_initial_schema`, owner-scoped repository, keyset pagination, and uniform error envelopes (PR #3).
- Verified the suite against a throwaway PostgreSQL 16 database in CI and locally with `TEST_DATABASE_URL`.

## What I learned

- The saved-item grain `(user_id, content_id)` stayed stable from the first migration; Project 1B only changes how `user_id` is resolved.
- `/health` remains a liveness check; persistence correctness lives in integration tests, not in the health handler.

## Assistance attribution

- : spec conformance merge, CI matrix split, and test hardening.

## Links

- [PR #3](https://github.com/rlancaster243/api-engineering-lab/pull/3)
- [ADR 0002](../decisions/0002-saved-item-ownership-grain.md)
