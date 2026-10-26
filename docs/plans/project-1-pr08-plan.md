# Plan: Project 1 PR-08 — Gate E qualification

## Context

PR-05 through PR-07 delivered auth, isolation, and database failure mapping. **PR-08** closes Project 1 with evidence of bounded implementation and regression repair (spec §7, Gate E).

## Deliverables

1. **`GET /ready`** — readiness probe executing `SELECT 1` via the request session factory. Success: `200 {"status":"ready"}`. Transient DB failure: `503 SERVICE_UNAVAILABLE` (same envelope as F-016). No authentication (ops probe, like `/health`).
2. **Tests** — happy path, 503 injection, no-auth under `external_jwt`, regression guard that readiness calls the probe (not pool heuristics).
3. **Spec amendment** — [PROJECT_1_ENGINEERING_FIDELITY.md](../PROJECT_1_ENGINEERING_FIDELITY.md) §4 route table.
4. **ADR 0009** (Proposed) — readiness vs liveness boundary.
5. **Evidence** — [docs/evidence/project-1-gate-e-pr08.md](../evidence/project-1-gate-e-pr08.md) documents the deliberate regression narrative and repair.

## Regression exercise (documented)

First draft returned `200` when the SQLAlchemy pool reported connections without running `SELECT 1`. Item routes could still fail with `503`. The fix wires `database_is_ready()` and a test that fails if the probe is bypassed.

## Out of scope

- Project 2 async jobs.
- Changing `/health` semantics.

## Verification

Same commands as PR-07; readiness tests run in the postgres integration job.
