# Project 1 — Gate E evidence (PR-08)

**Date:** 2026-10-26  
**Branch:** `cursor/project-1-pr08-qualification-530c`

## Bounded new behavior

- **`GET /ready`** added in `01-rest-api/app/ready.py` and `app/main.py`.
- Proves PostgreSQL accepts a round trip before an operator marks the service ready for traffic that depends on persistence.

## Deliberate regression and repair

| Step | Detail |
|------|--------|
| **Introduced defect** | Early draft treated "pool has a connection" as ready without executing SQL. |
| **Symptom** | `/ready` returned `200` while `POST /v1/items` could still return `503` when the database was unreachable. |
| **Diagnosis** | Compared liveness (`/health`) vs readiness; traced item failures to `OperationalError` on first query. |
| **Repair** | `database_is_ready()` runs `SELECT 1`; failures surface through the existing `DBAPIError` handler (PR-07). |
| **Regression test** | `test_ready_performs_a_query_not_a_pool_heuristic` in `tests/test_ready.py`. |

## Review talking points

- **Transaction boundary:** readiness uses a short-lived session, read-only probe, no commit.
- **Failure mapping:** shares F-016 `503` path with item routes; `/health` stays liveness-only.
- **Security:** `/ready` is unauthenticated by design; it exposes only availability, not data.

## Assistance attribution

Implementation and documentation AI-assisted on Cloud Agent; intended as a template for an unaided repeat of the regression-debug loop in interview prep.

## Commands (evidence)

```bash
cd 01-rest-api
uv sync --locked --extra dev
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rest_api_lab_test \
  REQUIRE_DATABASE=1 uv run pytest -q tests/test_ready.py
```
