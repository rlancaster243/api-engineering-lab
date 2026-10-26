# Plan: Project 1 PR-07 — failure mapping and operational polish

## Context

Project 1B (PR-05, PR-06) is merged. Spec **F-016** still required database outages to return **`503 SERVICE_UNAVAILABLE`** instead of **`500 INTERNAL_ERROR`**, with no false durable success.

## Scope (this PR)

1. Classify transient database failures (`OperationalError`, invalidated connections, selected SQLSTATEs) in [`01-rest-api/app/db_errors.py`](../01-rest-api/app/db_errors.py).
2. Register a `DBAPIError` handler in [`01-rest-api/app/errors.py`](../01-rest-api/app/errors.py); integrity errors that escape the repository remain **500**.
3. HTTP failure-injection tests in [`01-rest-api/tests/test_db_unavailable.py`](../01-rest-api/tests/test_db_unavailable.py) (F-016).
4. Update [ADR 0004](../decisions/0004-persistence.md) consequence text; learning-log note.

## Out of scope

- **PR-08** independent qualification exercise.
- Project 2 async jobs.

## Verification

```bash
cd 01-rest-api
uv sync --locked --extra dev
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rest_api_lab_test \
  REQUIRE_DATABASE=1 uv run pytest -q
```

Mutation: mapping `OperationalError` to `500` or letting failed writes return `201` must fail tests.
