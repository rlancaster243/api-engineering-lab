# Portfolio architecture

## Overview

`api-engineering-lab` is a **multi-project repository** (not a shared Python package). Three FastAPI applications live in sibling directories and evolve along a single learning path.

```
api-engineering-lab/
├── 01-rest-api/          # Phase 1: synchronous REST + Postgres
├── 02-async-job-api/     # Phase 2: job queue + status API
├── 03-api-hardening/     # Phase 3: security + resilience patterns
├── docs/                 # Architecture, ADRs, learning log
└── .github/workflows/    # CI runs pytest in each project
```

## Independence vs shared conventions

| Concern | Approach |
|---------|----------|
| Code reuse | None initially — copy/adapt patterns deliberately |
| Layout | Each project: `app/`, `tests/`, `pyproject.toml`, `.env.example` |
| Runtime | Separate process per module (`uvicorn app.main:app`) |
| Database | Per-project `DATABASE_URL`; schemas not shared yet |
| CI | One workflow, matrix over project directories |

## Shared CI

The root workflow runs Ruff and pytest in each project directory. Dedicated postgres integration jobs run for `01-rest-api` and `02-async-job-api` (`TEST_DATABASE_URL`, `REQUIRE_DATABASE=1`). Project `03` still exercises only health smoke tests in the matrix job.

## Evolution

Future options (not implemented yet):

- Extract a small internal `common` package once duplication hurts
- Split into separate repos if modules become standalone products
- Add compose file for Postgres + all three APIs for local demos

See [ADR 0001](../decisions/0001-repo-layout.md) for why this layout was chosen.
