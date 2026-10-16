# 03 — API hardening

## Learning goals

- Authenticate and authorize API consumers (API keys, JWT, or OAuth patterns)
- Apply rate limiting, timeouts, and safe defaults on public endpoints
- Improve observability: structured logging, metrics, health vs readiness
- Practice failure modes: dependency outages, partial degradation

## Tech stack

- Python 3.12+
- FastAPI + Uvicorn
- PostgreSQL via `DATABASE_URL` when persistence is needed
- pytest

## Run locally

```bash
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --port 8003
```

Health check:

```bash
curl http://127.0.0.1:8003/health
```

## Test

```bash
uv run pytest
```

## Environment

| Variable | Default (local) | Purpose |
|----------|-----------------|--------|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/api_hardening_lab` | Optional app data |
