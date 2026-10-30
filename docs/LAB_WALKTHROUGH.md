# API Engineering Lab — portfolio walkthrough

This page is the interview-oriented map of the repository. It synthesizes the three
completion records and the syllabus exit condition; the authoritative detail lives in
[`evidence/`](evidence/) and [`decisions/`](decisions/).

## What the three projects demonstrate

**Project 1 — `01-rest-api/` (auth + persistence).** A Saved Content API with PostgreSQL,
Alembic migrations, CRUD on `/v1/items`, signed pagination cursors, hard delete, and a
readiness probe that fails closed when the database is down. Project 1B adds caller
identity (`local_dev` or external JWT) and strict two-user isolation without changing the
`UNIQUE(user_id, content_id)` grain. Together this shows HTTP contract discipline,
persistence invariants, and ownership enforcement backed by real PostgreSQL integration
tests.

**Project 2 — `02-async-job-api/` (async jobs).** Durable work as rows in PostgreSQL:
`POST /v1/jobs` returns `202` after commit, workers claim with `FOR UPDATE SKIP LOCKED`,
leases and retries recover from crashes, a transactional outbox plus reconciler make
external side effects safe under reclaim, and `Idempotency-Key` prevents duplicate
submissions. Five syllabus falsification cases are covered by tests that run against a
live database—not mocks.

**Project 3 — `03-api-hardening/` (investigation + hardening).** A multi-tenant asset
registry on SQLite, seeded with defects for a blind investigation: reproduce a reported
symptom, trace to the earliest divergence, add a failing-before regression test, fix at
the right layer, and explain blast radius. Follow-up work closed exception leakage, batch
status semantics, and a split transaction on tag writes. The narrative value is the
investigation method and honest evidence levels—not a claim of production on-call
experience.

## Honest limits (read this in an interview)

- **AI assistance.** Implementation, tests, and much of the documentation were written
  with AI coding agents (Cursor Cloud Agents). Commits carry `Co-authored-by` and often
  `Assisted-by: Cursor`. Each pull request was reviewed and merged by the repository
  owner. The syllabus §14 gate measures **implementation**, not unaided delivery; see
  [`API_ENGINEERING_SYLLABUS.md`](API_ENGINEERING_SYLLABUS.md) §14 and the assistance
  sections in each completion record.
- **Evidence levels.** Documented, implemented, tested, and operationally validated are
  separate rungs. Project 1 and 2 integration evidence is **PostgreSQL**. Project 3 is
  **SQLite only** (ADR 0012); do not infer PostgreSQL locking or MVCC lessons from it.
- **Seeded defects (Project 3).** The service was written to be investigated; planted
  defects were known to exist before the search for location began. The strongest signal
  in that phase is the unplanted `APP_ENV` fail-open finding, not the cross-tenant read
  alone.
- **Lab scope.** No deployed production environment (ADR 0008). Project 2 is
  at-least-once with idempotent effects, not exactly-once delivery. Project 3 uses
  published local API keys under `APP_ENV=local`—a seeding rule, not an auth product.

## Run the whole lab from a clean checkout

### Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** (recommended; CI uses locked sync)
- **Docker** (or local PostgreSQL) for Projects 1 and 2
- **Ports:** `8001` / `5432` (Project 1), `8002` / `5433` (Project 2), `8003` (Project 3
  HTTP only; SQLite file on disk)

Clone and install per module—there is no single root `pyproject.toml`:

```bash
git clone https://github.com/rlancaster243/api-engineering-lab.git
cd api-engineering-lab
```

### Project 1 — REST API

```bash
cd 01-rest-api
docker compose up -d
cp .env.example .env
# Set CURSOR_SIGNING_KEY to at least 32 bytes (e.g. openssl rand -hex 32).
# For local exploration without JWT setup:
#   APP_ENV=local
#   AUTH_MODE=local_dev
set -a && . ./.env && set +a
uv sync --locked --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8001
```

Smoke checks:

```bash
curl -s http://127.0.0.1:8001/health
curl -s http://127.0.0.1:8001/ready
```

Tests (disposable DB; schema is rebuilt):

```bash
docker compose exec postgres createdb -U postgres rest_api_lab_test || true
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rest_api_lab_test
uv run ruff check . && uv run ruff format --check .
REQUIRE_DATABASE=1 uv run pytest -q
```

### Project 2 — Async job API

```bash
cd ../02-async-job-api
docker compose up -d    # Postgres on host port 5433
cp .env.example .env
set -a && . ./.env && set +a
uv sync --locked --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8002
# In another terminal, run the worker (see module README):
# uv run python -m app.worker
```

Tests:

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/async_job_api_lab
uv run ruff check . && uv run ruff format --check .
REQUIRE_DATABASE=1 uv run pytest -q
```

### Project 3 — API hardening

No PostgreSQL. **`APP_ENV` and `DATABASE_PATH` are required** with no default.

```bash
cd ../03-api-hardening
cp .env.example .env
set -a && . ./.env && set +a
uv sync --locked --extra dev
uv run uvicorn app.main:app --reload --port 8003
```

Use the documented keys in the module README (`local-dev-key-alpha` / `beta` when
`APP_ENV=local`). Tests use ephemeral SQLite files:

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

### Full verification (all modules)

From the repository root, matching CI discipline:

```bash
for dir in 01-rest-api 02-async-job-api 03-api-hardening; do
  echo "=== $dir ==="
  (
    cd "$dir" || exit 1
    uv sync --locked --extra dev
    uv run ruff check . && uv run ruff format --check .
    if [ "$dir" = "01-rest-api" ]; then
      export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/rest_api_lab_test}"
      REQUIRE_DATABASE=1 uv run pytest -q
    elif [ "$dir" = "02-async-job-api" ]; then
      export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql://postgres:postgres@localhost:5433/async_job_api_lab}"
      REQUIRE_DATABASE=1 uv run pytest -q
    else
      uv run pytest -q
    fi
  )
done
```

Start both compose stacks before running Project 1 and 2 tests. With `REQUIRE_DATABASE=1`,
a missing `TEST_DATABASE_URL` or unreachable Postgres fails the suite instead of skipping
integration tests.

## Evidence records and architecture decisions

| Phase | Completion record | Primary ADRs |
|-------|-------------------|--------------|
| Project 1 | [`evidence/project-1-completion.md`](evidence/project-1-completion.md) | [0002](decisions/0002-saved-item-ownership-grain.md)–[0009](decisions/0009-readiness-endpoint.md) |
| Project 2 | [`evidence/project-2-completion.md`](evidence/project-2-completion.md) | [0010](decisions/0010-postgres-job-store.md), [0011](decisions/0011-job-idempotency.md) |
| Project 3 | [`evidence/project-3-completion.md`](evidence/project-3-completion.md) | [0012](decisions/0012-sqlite-for-project-3.md) |

Syllabus gates, §14 qualification checklist, and exit condition:
[`API_ENGINEERING_SYLLABUS.md`](API_ENGINEERING_SYLLABUS.md) (§9 evidence register, §14
gates, exit condition at end of §14).

Gate E / readiness work for Project 1:
[`evidence/project-1-gate-e-pr08.md`](evidence/project-1-gate-e-pr08.md).
