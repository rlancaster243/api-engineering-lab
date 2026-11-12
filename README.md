# API Engineering Lab

A hands-on portfolio for learning production-grade API design in Python. Each module is an **independently runnable** FastAPI service with its own dependencies, tests, and documentation.

See the full 8-week implementation plan in [`docs/API_ENGINEERING_SYLLABUS.md`](docs/API_ENGINEERING_SYLLABUS.md).

## Roadmap (three phases)

| Phase | Directory | Milestones & Focus |
|-------|-----------|--------------------|
| 1 | [`01-rest-api/`](01-rest-api/) | **Milestone 0:** reproducible foundation, `/health` contract, lint + CI.<br>**Project 1A:** Saved Content CRUD (`POST/GET/PATCH/DELETE /v1/items`), PostgreSQL persistence, stable `UNIQUE(user_id, content_id)` ownership grain.<br>**Project 1B:** Authentication and per-user ownership enforcement (two-user isolation). Complete — see the [completion record](docs/evidence/project-1-completion.md). |
| 2 | [`02-async-job-api/`](02-async-job-api/) | **Project 2:** Durable async jobs (`202 Accepted`), status queries, worker leases, retries, submission idempotency, effect outbox, and crash recovery. Complete — see the [completion record](docs/evidence/project-2-completion.md). |
| 3 | [`03-api-hardening/`](03-api-hardening/) | **Project 3:** Multi-tenant asset registry — blind defect investigation, failing-before regression tests, and hardening (tenant isolation, fail-closed config, safe HTTP errors). Complete — see the [completion record](docs/evidence/project-3-completion.md). |

Progress through the modules in order; each phase builds on the verification discipline established in earlier phases.

## Prerequisites

- **Python 3.12+**
- **PostgreSQL** (local or container) — connection via `DATABASE_URL` (see each project's `.env.example`)
- **uv** (recommended) or **pip** for dependency install

## Quick start (one module)

Minimal smoke test only — for compose, migrations, signing keys, and full verification see **[Lab walkthrough](docs/LAB_WALKTHROUGH.md)**.

```bash
cd 01-rest-api
cp .env.example .env   # edit DATABASE_URL if needed; set CURSOR_SIGNING_KEY (32+ bytes) for full API
uv sync --locked --extra dev
uv run uvicorn app.main:app --reload --port 8001
curl http://127.0.0.1:8001/health
```

Lint and a quick test pass (integration tests need PostgreSQL — see walkthrough):

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Use ports `8001`, `8002`, and `8003` when running multiple modules side by side.

## Run CI locally

GitHub Actions runs **locked** `uv sync`, Ruff, and **postgres integration** jobs for Projects 1 and 2 (`REQUIRE_DATABASE=1`). The matrix jobs alone do not substitute for those integration runs.

**Canonical full-lab verification** (all three modules, matching CI discipline) is in [`docs/LAB_WALKTHROUGH.md`](docs/LAB_WALKTHROUGH.md) under **Full verification (all modules)**. Start both compose stacks before Project 1 and 2 tests.

Condensed from the walkthrough:

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

## Lab walkthrough

For a one-page portfolio narrative, honest limits (SQLite vs PostgreSQL, seeded defects),
and step-by-step commands to run all three modules from a clean checkout,
see **[Lab walkthrough](docs/LAB_WALKTHROUGH.md)**.

## Documentation

- [Action syllabus](docs/API_ENGINEERING_SYLLABUS.md)
- [Architecture overview](docs/architecture/README.md)
- [Architecture decisions](docs/decisions/)
- [Learning log](docs/learning-log/README.md)

## Tech stack

- Python 3.12+
- [FastAPI](https://fastapi.tiangolo.com/) per project
- PostgreSQL (env-driven; `/health` checks process responsiveness only and does not require a live DB)
- [Ruff](https://docs.astral.sh/ruff/) + [pytest](https://docs.pytest.org/) per project
- GitHub Actions CI at [`.github/workflows/ci.yml`](.github/workflows/ci.yml)

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
