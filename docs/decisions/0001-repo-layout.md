# ADR 0001: Monorepo directories vs separate repositories

## Status

Accepted

## Context

The API Engineering Lab is a learning portfolio with three sequential phases (REST, async jobs, hardening). We need a Git hosting layout that supports:

- Clear phase boundaries for the learner
- Independent runnable services per phase
- One CI entry point for quality gates
- Room to grow without premature abstraction

## Decision

Use a **single private repository** with **three top-level project directories** (`01-rest-api`, `02-async-job-api`, `03-api-hardening`), plus shared `docs/` and `.github/workflows/`.

Each project owns its own `pyproject.toml`, `app/`, and `tests/`. There is **no** shared installable package in the initial bootstrap.

## Consequences

### Positive

- One clone, one CI badge, one place for ADRs and learning logs
- Numbered prefixes communicate order without enforcing import coupling
- Easy to compare implementations across phases in one PR or review

### Negative

- No per-phase access control at the Git level (whole repo is visible)
- Dependency versions may drift between projects until consciously aligned
- CI time grows linearly with three test suites (acceptable at this scale)

## Alternatives considered

1. **Three separate repositories** — better isolation and independent release cadence, but more overhead for a solo learning track and duplicated CI/docs.
2. **Single FastAPI app with feature flags** — faster to run locally, but blurs learning boundaries and encourages shared state too early.
3. **Monorepo with a shared `packages/common` library** — good later; rejected for bootstrap to force explicit repetition while learning.

## References

- Root [README](../../README.md)
- [Architecture overview](../architecture/README.md)
