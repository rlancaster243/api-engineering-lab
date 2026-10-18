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
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8001
```

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
