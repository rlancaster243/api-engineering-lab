# 02 — Async job API

## Learning goals

- Model long-running work as jobs with stable IDs and status transitions
- Expose enqueue and poll/query endpoints without blocking HTTP workers
- Separate API process from worker process (patterns introduced in later exercises)
- Test API contracts with pytest before wiring a real queue

## Tech stack

- Python 3.12+
- FastAPI + Uvicorn
- PostgreSQL via `DATABASE_URL` for job state (future)
- pytest

## Run locally

```bash
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --port 8002
```

Health check:

```bash
curl http://127.0.0.1:8002/health
```

## Test

```bash
uv run pytest
```

## Environment

| Variable | Default (local) | Purpose |
|----------|-----------------|--------|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/async_job_api_lab` | Job store (future) |
