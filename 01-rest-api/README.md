# 01 — REST API (Saved Content Service)

## Progression

1. **Milestone 0 (Foundation):** Reproducible local install, `GET /health` liveness contract (`200 OK`, `{"status": "ok"}`), Ruff lint + formatting, HTTP tests, and PR CI gate.
2. **Project 1A (Persistence):** Saved Content CRUD (`/v1/items`), PostgreSQL + SQLAlchemy + Alembic migrations, and the stable `UNIQUE(user_id, content_id)` ownership grain using a deterministic seeded development user.
3. **Project 1B (Auth & Ownership):** Authenticated caller principal resolution and strict two-user isolation across read, list, update, and delete operations without changing the Project 1A data grain.

See [`../docs/API_ENGINEERING_SYLLABUS.md`](../docs/API_ENGINEERING_SYLLABUS.md) and [ADR 0002](../docs/decisions/0002-saved-item-ownership-grain.md) for full details.

## Tech stack

- Python 3.12+
- FastAPI + Pydantic + Uvicorn
- PostgreSQL via `DATABASE_URL` (introduced in Project 1A; not required for Milestone 0 `/health`)
- Ruff (lint + format) and pytest (`TestClient`)

## Run locally

```bash
cp .env.example .env
uv sync --locked --extra dev
uv run uvicorn app.main:app --reload --port 8001
```

`--locked` installs exactly what `uv.lock` records and fails if `pyproject.toml` has drifted from it. To change dependencies, edit `pyproject.toml`, run `uv lock`, and commit both files. See [ADR 0008](../docs/decisions/0008-execution-boundary.md).

Without uv:

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8001
```

Health check:

```bash
curl -i http://127.0.0.1:8001/health
```

Expected response: `200 OK` with body `{"status":"ok"}`. Note: `/health` proves **process responsiveness** only, not PostgreSQL or external dependency readiness.

## Lint and test

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Without uv:

```bash
ruff check .
ruff format --check .
pytest -q
```

## Environment

| Variable | Default (local) | Purpose |
|----------|-----------------|--------|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/rest_api_lab` | PostgreSQL connection string for Project 1A+ |
| `CURSOR_SIGNING_KEY` | none (generate your own, 32+ bytes) | HMAC key for pagination cursors ([ADR 0007](../docs/decisions/0007-pagination.md)); not read until the list route lands in PR-04 |

### Local PostgreSQL

Any PostgreSQL 16 reachable at `DATABASE_URL` works. This matches the CI service container (credentials are throwaway, local use only):

```bash
docker run --rm -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rest_api_lab -p 5432:5432 postgres:16
psql "postgresql://postgres:postgres@localhost:5432/rest_api_lab" -c "select version()"
```
