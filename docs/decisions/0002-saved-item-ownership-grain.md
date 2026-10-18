# ADR 0002: Stable saved-item ownership grain (`user_id`, `content_id`)

## Status

Accepted

## Context

Project 1 (`01-rest-api/`) builds a Saved Content API in two increments:

- **Project 1A:** CRUD endpoints (`/v1/items`) backed by PostgreSQL, SQLAlchemy, and Alembic migrations.
- **Project 1B:** Authentication and per-user authorization enforcement.

If Project 1A models saved items without an owner (`UNIQUE(content_id)`), adding multi-user support in Project 1B would change the fundamental table grain, require backfilling historical rows with a synthetic owner, replace the uniqueness constraint with `UNIQUE(user_id, content_id)`, and rewrite repository queries.

## Decision

Define the saved-item grain from **Project 1A onward** as:

> One row in `saved_items` represents **one user's saved reference to one piece of content**.

### Core attributes

| Column | Type / Constraint | Purpose |
|--------|-------------------|---------|
| `id` | Surrogate primary key (UUID or bigint) | Stable resource identifier for `/v1/items/{id}` |
| `user_id` | `NOT NULL` foreign key -> `users.id` | Owner of the saved item |
| `content_id` | `NOT NULL` string | Opaque external content identifier in a single documented namespace |
| `notes` | Nullable text | Optional mutable user note |
| `created_at` / `updated_at` | `NOT NULL` UTC timestamps | Audit and deterministic ordering |

### Invariant

`UNIQUE(user_id, content_id)` — different users may save the same `content_id`, and a single user cannot save the same `content_id` twice (returns `409 Conflict`).

### Phased principal resolution

- **Project 1A (local development only):** A deterministic seeded development user is created via migration/seed. The server resolves `user_id` internally to that fixed user; request payloads must **never** accept caller-supplied `user_id`.
- **Project 1B (authenticated mode):** Caller identity is resolved from verified authentication credentials, and every read/list/update/delete query scopes by `(id, user_id)`. The database schema and grain remain unchanged.

## Consequences

### Positive

- Zero grain migration between Project 1A and Project 1B.
- Uniqueness and foreign-key invariants are tested against PostgreSQL from the first persistence PR.
- Prevents mass-assignment vulnerabilities (`user_id` is never accepted in request bodies).

### Negative

- Project 1A requires a minimal `users` table and dev-user seed before full authentication exists.
- Fixed-user development mode is strictly non-production and must never be exposed publicly.

## References

- [Action Syllabus §4, §6, §7](../API_ENGINEERING_SYLLABUS.md)
- [ADR 0001: Monorepo layout](0001-repo-layout.md)
