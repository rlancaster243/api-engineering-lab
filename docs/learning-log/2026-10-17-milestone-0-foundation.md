# 2026-10-17 — Milestone 0 foundation (`01-rest-api`)

## What I did

- Reconciled root `README.md` and `01-rest-api/README.md` with the Action Syllabus so Project 1 explicitly covers Milestone 0 -> Project 1A (Saved Content CRUD + PostgreSQL) -> Project 1B (authentication & ownership enforcement).
- Recorded [ADR 0002](../decisions/0002-saved-item-ownership-grain.md) locking in the `UNIQUE(user_id, content_id)` saved-item grain from Project 1A onward.
- Pinned `01-rest-api` `GET /health` to a typed Pydantic `HealthResponse` returning `200 OK` with `{"status": "ok"}`.
- Strengthened `01-rest-api/tests/test_health.py` to assert status code `200`, `application/json` content type, exact `{"status": "ok"}` payload, and `405 Method Not Allowed` on `POST /health`.
- Added `ruff` (`E`, `F`, `I`, `UP`, `B`, Python 3.12 target) to project dev dependencies and `.github/workflows/ci.yml` (`ruff check .` + `ruff format --check .` + `pytest -q`).

## What I learned / verified

- **What `/health` proves:** The FastAPI/ASGI process is importable, routing is wired, and Pydantic response serialization returns `200 OK` with `{"status": "ok"}`.
- **What `/health` does NOT prove:** Database connectivity, schema migration state, or downstream dependency readiness. Those belong to Project 1A persistence tests and readiness checks.
- **Verification commands & output:**
  - `ruff check . && ruff format --check .` -> `All checks passed!`, `4 files already formatted`
  - `pytest -q` in `01-rest-api` -> `2 passed`

- **Next independent exercise (Project 1A):** Define `users` and `saved_items` SQLAlchemy models, Alembic migration, and `/v1/items` CRUD endpoints against PostgreSQL.

## Links

- [`01-rest-api/`](../../01-rest-api/)
- [ADR 0002: Stable saved-item ownership grain](../decisions/0002-saved-item-ownership-grain.md)
- [Action Syllabus](../API_ENGINEERING_SYLLABUS.md)
