# ADR 0009: Readiness endpoint (`GET /ready`)

## Status

Accepted (PR-08)

## Context

`GET /health` proves the ASGI process is up (spec §4, Milestone 0). Operators and CI often need a separate signal that PostgreSQL is reachable before routing persistence-backed traffic. Project 1 PR-08 (Gate E) adds that probe without changing liveness semantics.

## Decision

1. Expose **`GET /ready`** returning `200` with `{"status":"ready"}` only after `SELECT 1` succeeds through the application session factory.
2. On transient database failure, return **`503 SERVICE_UNAVAILABLE`** with the section 4.5 error envelope (reuses PR-07 classification).
3. **No authentication** on `/ready`, same class as `/health`.
4. **`GET /health` is unchanged** and must not connect to PostgreSQL.

## Consequences

### Positive

- Load balancers and deploy scripts can gate on readiness without exercising item auth or business logic.
- Failures align with item-route database outage behavior (F-016).

### Negative

- Every readiness check consumes a database connection; high-frequency probing should be rate-limited externally.
- A passing `/ready` does not prove migrations are current or JWKS is reachable.

## References

- [Project 1 engineering fidelity specification](../PROJECT_1_ENGINEERING_FIDELITY.md) §4
- [Gate E evidence](../evidence/project-1-gate-e-pr08.md)
- [ADR 0004](0004-persistence.md), [ADR 0008](0008-execution-boundary.md)
