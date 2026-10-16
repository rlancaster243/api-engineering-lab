# 01 — REST API

## Learning goals

- Design resource-oriented HTTP APIs with clear status codes and error shapes
- Validate request/response payloads with Pydantic
- Connect FastAPI to PostgreSQL (migrations and repositories in later exercises)
- Write pytest tests with FastAPI's `TestClient`

## Tech stack

- Python 3.12+
- FastAPI + Uvicorn
- PostgreSQL via `DATABASE_URL` (not required for the health smoke test)
- pytest

## Run locally

```bash
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --port 8001
```

Health check:

```bash
curl http://127.0.0.1:8001/health
```

## Test

```bash
uv run pytest
```

Without uv:

```bash
pip install -e ".[dev]"
pytest
```

## Environment

| Variable | Default (local) | Purpose |
|----------|-----------------|--------|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/rest_api_lab` | Future DB sessions |
