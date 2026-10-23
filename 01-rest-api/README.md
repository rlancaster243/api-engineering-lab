# 01 — REST API (Saved Content Service)

## Progression

1. **Milestone 0 (Foundation):** Reproducible local install, `GET /health` liveness contract (`200 OK`, `{"status": "ok"}`), Ruff lint + formatting, HTTP tests, and PR CI gate.
2. **Project 1A (Persistence):** Saved Content CRUD (`/v1/items`), PostgreSQL + SQLAlchemy + Alembic migrations, and the stable `UNIQUE(user_id, content_id)` ownership grain using a deterministic seeded development user.
3. **Project 1B (Auth & Ownership):** Authenticated caller principal resolution and strict two-user isolation across read, list, update, and delete operations without changing the Project 1A data grain.

See [`../docs/API_ENGINEERING_SYLLABUS.md`](../docs/API_ENGINEERING_SYLLABUS.md) and [ADR 0002](../docs/decisions/0002-saved-item-ownership-grain.md) for full details.

## Tech stack

- Python 3.12+
- FastAPI + Pydantic + Uvicorn
- PostgreSQL 16 via SQLAlchemy 2 (psycopg 3) and Alembic migrations
- Ruff (lint + format) and pytest, with integration tests against a real PostgreSQL

The behavior follows [the Project 1 spec](../docs/PROJECT_1_ENGINEERING_FIDELITY.md) and ADRs [0003](../docs/decisions/0003-http-contract.md) (HTTP contract), [0004](../docs/decisions/0004-persistence.md) (persistence), [0006](../docs/decisions/0006-hard-delete.md) (hard delete) and [0007](../docs/decisions/0007-pagination.md) (pagination).

## Run locally

Start PostgreSQL (credentials are throwaway, local use only):

```bash
docker compose up -d        # or: docker run --rm -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rest_api_lab -p 5432:5432 postgres:16
```

Configure, migrate, run:

```bash
cp .env.example .env        # then set CURSOR_SIGNING_KEY; startup fails without it
set -a && . ./.env && set +a
uv sync --locked --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8001
```

`--locked` installs exactly what `uv.lock` records and fails if `pyproject.toml` has drifted from it. To change dependencies, edit `pyproject.toml`, run `uv lock`, and commit both files. See [ADR 0008](../docs/decisions/0008-execution-boundary.md).

Startup fails closed when configuration is missing or unsafe: no `DATABASE_URL`, no or short `CURSOR_SIGNING_KEY`, or `AUTH_MODE=local_dev` outside `APP_ENV=local`/`test`.

```bash
curl -i http://127.0.0.1:8001/health
curl -s -X POST http://127.0.0.1:8001/v1/items -H 'content-type: application/json' \
  -d '{"content_id":"article-042","title":"Distributed systems notes"}'
curl -s http://127.0.0.1:8001/v1/items
```

`/health` proves **process responsiveness** only; it does not touch PostgreSQL. In `local_dev` mode every item belongs to the seeded development owner, and the API never accepts `user_id`.

Upgrading a database created from the earlier, unmerged `0001_saved_items` revision fails with an unknown-revision error. Recreate it (`docker compose down -v`).

## Lint and test

The PostgreSQL tests drop and rebuild the schema, so they use **`TEST_DATABASE_URL`**, never `DATABASE_URL`. Point it at a database you can lose:

```bash
docker compose exec postgres createdb -U postgres rest_api_lab_test
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rest_api_lab_test
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Without `TEST_DATABASE_URL` the PostgreSQL tests skip and the contract tests still run. With `REQUIRE_DATABASE=1` (CI sets it) a missing database is a failure instead of a skip.

## Environment

| Variable | Default (local) | Purpose |
|----------|-----------------|--------|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/rest_api_lab` | Application and Alembic database. The plain `postgresql://` form is given the psycopg driver automatically, so the same URL works with `psql`. |
| `AUTH_MODE` | `external_jwt` | `local_dev` serves requests as the seeded development owner; `external_jwt` rejects item requests until token verification (PR-05). |
| `APP_ENV` | none | Must be `local` or `test` for `AUTH_MODE=local_dev`. |
| `CURSOR_SIGNING_KEY` | none (generate your own, 32+ bytes) | HMAC key for pagination cursors ([ADR 0007](../docs/decisions/0007-pagination.md)). Required at startup. |
| `TEST_DATABASE_URL` | none | Disposable database for the PostgreSQL test suite. |
| `REQUIRE_DATABASE` | unset | `1` makes the suite fail, not skip, without `TEST_DATABASE_URL`. |
