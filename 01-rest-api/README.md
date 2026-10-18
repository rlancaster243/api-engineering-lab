# 01 — REST API (Saved Content Service)

## Progression

1. **Milestone 0 (Foundation):** Reproducible local install, `GET /health` liveness contract (`200 OK`, `{"status": "ok"}`), Ruff lint + formatting, HTTP tests, and PR CI gate.
2. **Project 1A (Persistence):** Saved Content CRUD (`/v1/items`), PostgreSQL + SQLAlchemy + Alembic migrations, and the stable `UNIQUE(user_id, content_id)` ownership grain using a deterministic seeded development user.
3. **Project 1B (Auth & Ownership):** Authenticated caller principal resolution and strict two-user isolation across read, list, update, and delete operations without changing the Project 1A data grain.

See [`../docs/API_ENGINEERING_SYLLABUS.md`](../docs/API_ENGINEERING_SYLLABUS.md) and [ADR 0002](../docs/decisions/0002-saved-item-ownership-grain.md) for full details.

## Tech stack

- Python 3.12+
- FastAPI + Pydantic + Uvicorn
- PostgreSQL via `DATABASE_URL` (SQLAlchemy URL, `postgresql+psycopg://...`)
- Alembic migrations
- Ruff (lint + format) and pytest (`TestClient` against PostgreSQL for `/v1/items`)

## Run locally

Start Postgres (matches `.env.example`):

```bash
docker compose up -d
cp .env.example .env
```

Apply migrations and run the API:

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8001
```

Without uv:

```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8001
```

`GET /health` proves process responsiveness only. It does not connect to PostgreSQL.

```bash
curl -i http://127.0.0.1:8001/health
```

Saved items (owner is the seeded development user; the API does not accept `user_id`):

```bash
curl -s -X POST http://127.0.0.1:8001/v1/items \
  -H 'content-type: application/json' \
  -d '{"content_id":"verse-1","notes":"first"}'

curl -s http://127.0.0.1:8001/v1/items
```

## Lint and test

Item tests use `DATABASE_URL`. With Postgres from compose:

```bash
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/rest_api_lab
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

If `DATABASE_URL` is unset, Postgres integration tests skip and the health tests still run. CI for this project always sets `DATABASE_URL`.

## Environment

| Variable | Default (local) | Purpose |
|----------|-----------------|--------|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/rest_api_lab` | SQLAlchemy connection string for `/v1/items` |
