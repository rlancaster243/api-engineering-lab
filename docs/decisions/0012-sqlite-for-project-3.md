# ADR 0012: SQLite for Project 3

## Status

Proposed

## Context

`03-api-hardening/` is a service built to be investigated: a working multi-tenant asset
registry whose read, write and batch paths can be traced end to end. It needs persistence
that is real — schema, constraints, commit boundaries — because the behaviour being
studied happens where a request meets stored state.

The alternative is PostgreSQL, which Projects 1 and 2 use. That choice brings a service
container, a migration tool, a second CI job, and connection configuration, none of which
the investigation exercises. `01-rest-api` already demonstrates PostgreSQL persistence,
Alembic migrations and the `REQUIRE_DATABASE=1` skip-to-failure discipline; repeating it
here would add infrastructure weight without adding evidence.

## Decision

1. **Storage is a single SQLite file accessed through the stdlib `sqlite3` module.** No
   ORM, no additional dependency. `pyproject.toml` is unchanged by this decision.
2. **The schema is created from `app/db.py:SCHEMA` at startup** with
   `CREATE TABLE IF NOT EXISTS`, and the tests create it the same way against a fresh
   temporary file. There is no migration history because there is no deployed predecessor
   to migrate from; a change to the schema in this module is a change to that string.
3. **One connection per request**, opened by the `get_connection` dependency and closed in
   `finally`. Connections are never shared between requests; `check_same_thread=False`
   exists only so a request's own connection can move between the dependency and the
   threadpool worker that runs the endpoint.
4. **Foreign keys are enabled per connection** (`PRAGMA foreign_keys = ON`), because
   SQLite defaults them off and the schema relies on `ON DELETE CASCADE` and on
   `asset_tags.asset_id` referencing a real asset.
5. **Timestamps are ISO-8601 UTC strings written by the application**, with
   microsecond precision, because SQLite has no native timestamp type and lexicographic
   order on this format is chronological order.
6. **Contention is a `503`, not a hang.** The connection uses `timeout=5.0`; a
   `database is locked` error is mapped to `SERVICE_UNAVAILABLE`.
7. **`03-api-hardening` stays in the existing lint/pytest matrix job.** No new job and no
   service container, so the module adds one line of CI cost, not a fourth one.

## Consequences

### Positive

- The module runs from a clean checkout with `uv sync` and nothing else. No `docker
  compose up`, no `DATABASE_URL`, no migration step before the first request.
- Tests run in under a second against real files, which keeps the whole suite cheap
  enough to run on every change.
- The behaviour under study is HTTP-contract behaviour. It is fully present here, and it
  is easier to see without a database protocol in the way.

### Negative — recorded deliberately

- **This module does not exercise PostgreSQL transaction or locking semantics.** Where a
  defect here is described as leaving "partial state", it is a *commit-boundary* defect:
  two committed statements where one atomic unit was needed. PostgreSQL would show the
  same defect through the same mechanism, but the reverse is not true — a defect that
  depends on MVCC, `SELECT ... FOR UPDATE`, serialization failures or `READ COMMITTED`
  visibility cannot occur here and is therefore not demonstrated here.
- SQLite serialises writers over the whole file. A test that passes here says nothing
  about write concurrency at load.
- `sqlite3` error types and messages differ from psycopg's. Error-mapping code in
  `app/errors.py` is specific to this module and is not portable.
- Row-level types are loose: no native `timestamp`, `uuid` or `boolean`. Values are
  validated at the schema layer and stored as text or integers.
- There is no second process holding the database open, so a test cannot observe lock
  contention from a real concurrent writer without extra machinery.

### Neutral

- Adopting PostgreSQL later means replacing `app/db.py`, the repository's SQL, and the
  error mapping, and adding Alembic. The service, router and schema layers would not
  change, because the repository is the only place that speaks SQL.

## References

- `03-api-hardening/README.md` — the service this decision governs
- [ADR 0004](0004-persistence.md) — PostgreSQL persistence for Project 1
- [Syllabus](../API_ENGINEERING_SYLLABUS.md) §9, §14
