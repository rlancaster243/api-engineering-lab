# ADR 0004: PostgreSQL persistence, migrations and transaction boundaries

Logical ID: P1-ADR-003 in [`PROJECT_1_ENGINEERING_FIDELITY.md`](../PROJECT_1_ENGINEERING_FIDELITY.md) §11; refines syllabus ADR-003. Implemented by `01-rest-api/alembic/versions/0001_initial_schema.py`, `app/repository.py` and `app/db.py`.

## Status

Accepted

## Context

Saved items must survive restarts, stay unique per owner under concurrency, and never be reported as saved unless they are durable (spec INV-01 to INV-08). [ADR 0008](0008-execution-boundary.md) fixes PostgreSQL 16 in both local and CI execution.

## Decision

1. **Schema is defined by one Alembic migration whose SQL is the spec §3.2 DDL verbatim.** ORM mappings in `app/models.py` only name tables and columns; constraints live in the migration. This keeps the spec, the migration and the database comparable line by line.
2. **The database enforces the invariants**, not the application: `UNIQUE(user_id, content_id)`, the external-identity pair check, one local seed user (`uq_users_local_seed`), and CHECK constraints that accept exactly the values the API accepts (same trim set, URL and length limits). A pre-insert existence check is not used; it cannot be correct under concurrency.
3. **The database is the only time source.** `created_at` and `updated_at` come from `now()`; the application never sends timestamps.
4. **Every operation is one owner-scoped statement**, committed before the method returns: `INSERT ... RETURNING`, `UPDATE ... WHERE id AND user_id RETURNING`, `DELETE ... WHERE id AND user_id RETURNING`. The ownership check and the change cannot be split by a concurrent request, and `201`/`200`/`204` are returned only after commit.
5. **Only `uq_user_content` means "already saved".** A unique violation on that constraint maps to `409`; every other integrity error (primary key, foreign key, CHECK) propagates and becomes `500`.
6. **Sessions are per request** and closed in a `finally`; closing rolls back anything uncommitted.
7. **Drivers:** SQLAlchemy 2 with psycopg 3. `DATABASE_URL` may use the plain `postgresql://` form, so the same value works for the app, Alembic and `psql`.

## Consequences

### Positive

- Concurrent duplicate saves produce exactly one row and one `201` (tested with 8 racing writers).
- Writes that bypass the API (seed scripts, migrations, a future worker) are held to the same rules.
- `updated_at` cannot be earlier than `created_at` because of clock skew between hosts.

### Negative

- Two sources state the schema: the migration and the ORM mappings. They are checked by the integration tests, not by autogeneration; Alembic autogenerate does not compare CHECK constraints.
- Raw SQL in the migration bypasses SQLAlchemy's dialect abstraction. PostgreSQL is the only supported engine (ADR 0008), so this is accepted.
- Transient database outages surface as `503 SERVICE_UNAVAILABLE` via `app/db_errors.py` and the `DBAPIError` handler (F-016, PR-07). Other database errors remain `500`.
- `created_at` is transaction-start time; ADR 0007 documents the effect on pagination.

## References

- [Project 1 engineering fidelity specification](../PROJECT_1_ENGINEERING_FIDELITY.md) §2.1, §3, §6, §8
- [ADR 0002](0002-saved-item-ownership-grain.md), [ADR 0003](0003-http-contract.md), [ADR 0007](0007-pagination.md), [ADR 0008](0008-execution-boundary.md)
