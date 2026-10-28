# API Engineering Lab

A hands-on portfolio for learning production-grade API design in Python. Each module is an **independently runnable** FastAPI service with its own dependencies, tests, and documentation.

See the full 8-week implementation plan in [`docs/API_ENGINEERING_SYLLABUS.md`](docs/API_ENGINEERING_SYLLABUS.md).

## Roadmap (three phases)

| Phase | Directory | Milestones & Focus |
|-------|-----------|--------------------|
| 1 | [`01-rest-api/`](01-rest-api/) | **Milestone 0:** reproducible foundation, `/health` contract, lint + CI.<br>**Project 1A:** Saved Content CRUD (`POST/GET/PATCH/DELETE /v1/items`), PostgreSQL persistence, stable `UNIQUE(user_id, content_id)` ownership grain.<br>**Project 1B:** Authentication and per-user ownership enforcement (two-user isolation). |
| 2 | [`02-async-job-api/`](02-async-job-api/) | **Project 2:** Durable async jobs (`202 Accepted`), status queries, worker leases, retries, submission idempotency, effect outbox, and crash recovery. Complete — see the [completion record](docs/evidence/project-2-completion.md). |
| 3 | [`03-api-hardening/`](03-api-hardening/) | **Project 3:** Investigate and repair a real defect/failure mode in an existing API with failing-before/passing-after regression tests. |

Progress through the modules in order; each phase builds on the verification discipline established in earlier phases.

## Prerequisites

- **Python 3.12+**
- **PostgreSQL** (local or container) — connection via `DATABASE_URL` (see each project's `.env.example`)
- **uv** (recommended) or **pip** for dependency install

## Quick start (one module)

```bash
cd 01-rest-api
cp .env.example .env   # edit DATABASE_URL if needed
uv sync --extra dev    # or: pip install -e ".[dev]"
uv run uvicorn app.main:app --reload --port 8001
curl http://127.0.0.1:8001/health
```

Run lint and tests:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Use ports `8001`, `8002`, and `8003` when running multiple modules side by side.

## Run CI locally

From the repository root, exercise the same checks as GitHub Actions:

```bash
for dir in 01-rest-api 02-async-job-api 03-api-hardening; do
  echo "=== $dir ==="
  (cd "$dir" && pip install -e ".[dev]" && ruff check . && ruff format --check . && pytest -q)
done
```

With **uv**:

```bash
for dir in 01-rest-api 02-async-job-api 03-api-hardening; do
  echo "=== $dir ==="
  (cd "$dir" && uv sync --extra dev && uv run ruff check . && uv run ruff format --check . && uv run pytest -q)
done
```

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

Private learning repository — all rights reserved unless otherwise noted.
