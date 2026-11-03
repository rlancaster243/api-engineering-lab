# ADR 0008: Project 1 execution boundary (local + CI)

Logical ID: P1-ADR-007 in [`PROJECT_1_ENGINEERING_FIDELITY.md`](../PROJECT_1_ENGINEERING_FIDELITY.md) §11. Numbers `0003`-`0007` are reserved there for the other Project 1 decisions.

## Status

Accepted

## Context

Project 1 (`01-rest-api/`) is a production-disciplined REST API built to demonstrate engineering practice, not to serve users. Requirement REQ-05 selects **local + CI** execution: no public deployment, no managed production authentication, no standing staging environment.

The persistence design depends on PostgreSQL behavior that other engines do not share: `UNIQUE` treating `NULL`s as distinct, partial unique indexes, `INSERT ... ON CONFLICT`, row-value comparison for keyset pagination, and SQLSTATE codes (`23505`, `23503`, `23514`) that the API maps to HTTP statuses. Tests that run against a different engine would not prove those properties.

Before this decision, `.github/workflows/ci.yml` installed dependencies with `pip install -e ".[dev]"`, which resolves the newest compatible versions on every run, and it kept a commented-out `services:` stub nested under `steps`, where GitHub Actions does not accept it.

## Decision

1. **Execution environments are exactly two:** a developer machine and GitHub Actions. There is no deployed environment, and no acceptance criterion depends on one.
2. **Database:** PostgreSQL 16, reached through `DATABASE_URL`. Persistence tests run against a real PostgreSQL instance. Mocked HTTP responses or another engine (for example SQLite) do not substitute for them.
3. **CI database:** dedicated **postgres integration** jobs for `01-rest-api` and `02-async-job-api` (PostgreSQL 16 service containers at the job level, migrations + full `pytest` with `REQUIRE_DATABASE=1`). A separate **lint & pytest** matrix job runs per module (Ruff and tests that do not require the integration database setup). `03-api-hardening` uses SQLite only; its CI job runs Ruff and `pytest` with `APP_ENV=test` and no database service. Credentials in CI are throwaway values for ephemeral containers and are not secrets.
4. **Dependencies:** each project commits a `uv.lock`. CI installs with `uv sync --locked`, which fails when `pyproject.toml` and `uv.lock` disagree. The uv version is pinned in the workflow to the version that produced the lockfiles. Dependency changes are made in `pyproject.toml`, followed by `uv lock` and committing the result.
5. **Single-user development mode** (spec §5.3) is accepted only when `APP_ENV` is `local` or `test`, and never as a fallback for failed token verification.
6. **Evidence:** a check passing in CI is evidence that the checked commands succeeded on that commit. It is not evidence of production readiness, and no document may claim it is.

## Consequences

### Positive

- Database-dependent invariants are tested on the engine that enforces them.
- CI installs the same dependency set every time, and a lockfile that has drifted fails the build instead of silently changing the environment.
- No deployment surface means no production secrets, uptime obligations, or public exposure of the development-mode identity.

### Negative

- No staging or production behavior is observed. Operational claims stop at "demonstrated locally and in CI".
- Three lockfiles must be kept current, and the pinned uv version must be bumped deliberately.
- Postgres integration coverage requires maintaining service containers and migration steps on the dedicated CI jobs; adding another Postgres-backed module means extending that pattern.

## References

- [Project 1 engineering fidelity specification](../PROJECT_1_ENGINEERING_FIDELITY.md) §5.3, §7 (PR-00), §9, §11
- [ADR 0001: Monorepo layout](0001-repo-layout.md)
- [ADR 0002: Stable saved-item ownership grain](0002-saved-item-ownership-grain.md)
- [Action Syllabus §12](../API_ENGINEERING_SYLLABUS.md)
