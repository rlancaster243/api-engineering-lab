# 02 — Async job API

## Progression

1. **PR-2-00 (Foundation):** Reproducible install, `DATABASE_URL`, Alembic, `GET /health`, CI postgres job.
2. **PR-2-01+:** Job schema, `POST /v1/jobs` → 202, worker, idempotency (see [Project 2 plan](../docs/plans/project-2-plan.md)).

## Run locally

```bash
docker compose up -d
cp .env.example .env
set -a && . ./.env && set +a
uv sync --locked --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8002
curl http://127.0.0.1:8002/health
```

Postgres is published on host port **5433** to avoid clashing with Project 1 on 5432.

## Lint and test

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/async_job_api_lab
uv run ruff check . && uv run ruff format --check .
REQUIRE_DATABASE=1 uv run pytest -q
```

Without `TEST_DATABASE_URL`, migration integration tests skip (CI postgres job sets `REQUIRE_DATABASE=1`).

## Environment

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Required at app startup and for Alembic |
| `TEST_DATABASE_URL` | Disposable DB for integration tests |
| `REQUIRE_DATABASE` | `1` fails CI if database tests are skipped |
| `APP_ENV` | Reserved for later worker/API modes |

## ADRs

- [0010 Postgres job store](../docs/decisions/0010-postgres-job-store.md) (Proposed)
- [0011 Job idempotency](../docs/decisions/0011-job-idempotency.md) (Proposed)
