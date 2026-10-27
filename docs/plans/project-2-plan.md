# Plan: Project 2 — async job API

## Context

Project 1 (`01-rest-api/`) is complete on `main`. Project 2 (`02-async-job-api/`) delivers durable background work with HTTP submission and polling (syllabus §8).

## PR sequence

| PR | Branch focus | Deliverable |
|----|----------------|-------------|
| **PR-2-00** | Foundation | **Done** ([#11](https://github.com/rlancaster243/api-engineering-lab/pull/11)): `uv.lock`, config, SQLAlchemy, Alembic, compose, CI postgres job, ADRs 0010–0011 (Proposed) |
| **PR-2-01** | Schema + submit | **Done** (PR TBD): `jobs` table, `POST /v1/jobs` → 202 after commit, `GET /v1/jobs/{id}` |
| PR-2-02 | Worker | Claim/lease protocol, separate worker process |
| PR-2-03 | Recovery | Retries, lease expiry, crash falsification tests |
| PR-2-04 | Idempotency | `Idempotency-Key` header semantics per ADR 0011 |

## State machine (target)

`PENDING` → `RUNNING` → `SUCCEEDED` | `FAILED` (with explicit `attempt` / `max_attempts`).

## Falsification cases (syllabus)

1. Duplicate submit after timeout → no duplicate work  
2. Two workers → one winner  
3. Worker crash → lease expiry recovery  
4. Side effect vs persistence failure → safe retry policy (ADR 0011)  
5. DB down on accept → no false 202  

## Verification

```bash
cd 02-async-job-api
uv sync --locked --extra dev
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/async_job_api_lab \
  REQUIRE_DATABASE=1 uv run pytest -q
```


## Open questions

- **Unauthenticated submission:** `POST /v1/jobs` requires no auth token at lab scale. Recorded as a known assumption; auth would land separately if needed.
