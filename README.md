# API Engineering Lab

A hands-on portfolio for learning production-grade API design in Python. Each module is an **independently runnable** FastAPI service with its own dependencies, tests, and documentation.

## Roadmap (three phases)

| Phase | Directory | Focus |
|-------|-----------|--------|
| 1 | [`01-rest-api/`](01-rest-api/) | REST fundamentals: resources, validation, persistence |
| 2 | [`02-async-job-api/`](02-async-job-api/) | Async jobs: enqueue, status, workers |
| 3 | [`03-api-hardening/`](03-api-hardening/) | Hardening: auth, rate limits, observability |

Progress through the modules in order; later phases assume familiarity with earlier patterns.

## Prerequisites

- **Python 3.12+**
- **PostgreSQL** (local or container) — connection via `DATABASE_URL` (see each project's `.env.example`)
- **uv** (recommended) or **pip** for dependency install

## Quick start (one module)

```bash
cd 01-rest-api
cp .env.example .env   # edit DATABASE_URL if needed
uv sync                # or: pip install -e ".[dev]"
uv run uvicorn app.main:app --reload --port 8001
curl http://127.0.0.1:8001/health
```

Run tests:

```bash
uv run pytest
```

Use ports `8001`, `8002`, and `8003` when running multiple modules side by side.

## Run CI locally

From the repository root, exercise the same checks as GitHub Actions:

```bash
for dir in 01-rest-api 02-async-job-api 03-api-hardening; do
  echo "=== $dir ==="
  (cd "$dir" && pip install -e ".[dev]" && pytest -q)
done
```

With **uv**:

```bash
for dir in 01-rest-api 02-async-job-api 03-api-hardening; do
  echo "=== $dir ==="
  (cd "$dir" && uv sync --extra dev && uv run pytest -q)
done
```

## Documentation

- [Architecture overview](docs/architecture/README.md)
- [Architecture decisions](docs/decisions/)
- [Learning log](docs/learning-log/README.md)

## Tech stack

- Python 3.12+
- [FastAPI](https://fastapi.tiangolo.com/) per project
- PostgreSQL (env-driven; health checks do not require a live DB yet)
- [pytest](https://docs.pytest.org/) per project
- GitHub Actions CI at [`.github/workflows/ci.yml`](.github/workflows/ci.yml)

## License

Private learning repository — all rights reserved unless otherwise noted.
