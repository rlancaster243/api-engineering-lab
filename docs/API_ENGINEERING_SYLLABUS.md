# API Engineering Lab — Action Syllabus

**Version:** 1.0  
**Status:** Proposed implementation plan (not evidence of completion)  
**Repository:** [rlancaster243/api-engineering-lab](https://github.com/rlancaster243/api-engineering-lab)  
**Visibility:** Private  
**Primary stack:** Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy, Alembic, Pydantic, pytest, GitHub Actions  
**Planning horizon:** Eight weeks, approximately 6–8 focused hours per week

## 1. Mission and success criteria

Develop demonstrable backend/API engineering capability through implementation, verification, maintenance, and clear technical explanation. The goal is not to produce additional architecture documents alone.

The candidate should be able to independently implement a bounded endpoint, model data correctly, enforce access controls, test persistence and failure behavior, build a durable asynchronous workflow, debug an existing service, and explain engineering tradeoffs.

Prior architectural exposure to FastAPI, Project Ezra, and DEOS is relevant context, **not proof of independent implementation ability or runtime correctness**. Record whether each exercise was independently completed or AI-assisted.

### Working principles

1. **Contract before implementation:** define resource identity, request/response schema, status codes, validation, failure behavior, and tests.
2. **Grain before queries:** define what a persisted row represents before creating keys and constraints.
3. **Smallest viable system:** do not introduce Kafka, Kubernetes, Redis, or a permanent staging branch without a demonstrated requirement.
4. **Failure paths count:** test invalid input, missing resources, duplication, database errors, concurrency, partial failure, and recovery when relevant.
5. **Evidence over assertions:** distinguish documented, implemented, tested, demonstrated, and operationally verified behavior.
6. **Security boundary:** keep the unauthenticated single-user API restricted to local development; never expose it as a publicly reachable service.
7. **Independent performance:** include unaided implementation and debugging exercises, and record the assistance received.

## 2. Confirmed repository baseline and intended structure

The repository currently has a `main` default branch, a root `README.md`, `.gitignore`, `.github/`, `docs/`, and three numbered project directories. The README describes independently runnable services with their own dependencies, tests, and documentation. This syllabus preserves that structure and **does not claim application implementations or passing tests already exist**.

```text
api-engineering-lab/
├── .github/workflows/
├── 01-rest-api/
├── 02-async-job-api/
├── 03-api-hardening/
├── docs/
│   ├── architecture/
│   ├── decisions/
│   ├── learning-log/
│   └── API_ENGINEERING_SYLLABUS.md
├── .gitignore
└── README.md
```

**Branch policy [PROPOSED]:** `main` is authoritative; implement changes on short-lived `feature/*` or `fix/*` branches, validate with pull-request CI, then merge. A staging **environment** may be introduced later without adding a permanent staging **branch**.

**Documentation alignment [OPEN]:** The existing root README lists hardening/authentication together under Phase 3. This plan puts **ownership enforcement and authentication in Project 1B**, before the asynchronous job project. Reconcile the README when Project 1 work begins; no existing README changes are implied by this syllabus commit.

## 3. Project sequence

| Phase | Directory | Engineering focus | Completion evidence |
|---|---|---|---|
| Milestone 0 | `01-rest-api/` | Reproducible development workflow, health endpoint, tests, CI | Clean checkout runs and CI passes |
| Project 1A | `01-rest-api/` | CRUD, PostgreSQL persistence, stable ownership grain | Migrations and DB integration tests |
| Project 1B | `01-rest-api/` | Authentication and authorization enforcement | Two-user isolation and denial tests |
| Project 2 | `02-async-job-api/` | Durable jobs, workers, retries, idempotency, recovery | Concurrency and crash-recovery tests |
| Project 3 | `03-api-hardening/` | Investigate and repair an existing API failure | Reproduction, failing-before/passing-after regression test |

The sequence matters more than the calendar. Do not advance past a critical failing acceptance gate.

## 4. Architecture decision ADR-001 — stable ownership grain

**Status:** Proposed for adoption.

**Decision:** One saved-item row represents **one user's saved reference to one piece of content**, from the first persistence milestone onward.

| Attribute | Meaning |
|---|---|
| `id` | Stable resource primary key |
| `user_id` | Owner identifier, NOT NULL, references a user |
| `content_id` | Opaque content identifier in a defined namespace |
| `notes` | Optional mutable metadata |
| `created_at` / `updated_at` | Consistently generated timestamps |

**Invariant:** `UNIQUE(user_id, content_id)`. Different users may save the same content; one user cannot save the same content twice. Use a separate resource `id` so the API identifier is stable. If multiple content providers are later introduced, revisit whether a `content_source` or namespace field is required.

**Development-mode compromise:** Project 1A includes a fixed, seeded development user. The API supplies the owner internally; requests must not accept arbitrary `user_id` values. Project 1B replaces fixed identity resolution with authenticated caller identity and enforces ownership. **The data grain remains unchanged.**

**Rejected alternative:** Deferring `user_id` entirely would change uniqueness from `content_id` to `(user_id, content_id)` later, requiring historical owner attribution and altering every query. This can be a useful migration exercise but is unnecessary risk for the first API.

**Boundary:** Fixed-user mode is **not** a production or internet-facing authentication strategy.

## 5. Milestone 0 — development foundation (Week 1)

### Objective

Demonstrate an independently runnable FastAPI application and an automated verification loop.

### Tasks

- [ ] Inspect the scaffold before editing; preserve existing modules and configuration.
- [ ] Verify per-module Python dependencies and reproducible installation.
- [ ] Implement `GET /health` in Project 1 (or verify and test an existing implementation).
- [ ] Define response `200 OK` with `{"status":"ok"}`.
- [ ] Write an HTTP-level pytest test for status and response body.
- [ ] Configure lint and test commands.
- [ ] Make GitHub Actions run those checks on pull requests.
- [ ] Create and review a bounded PR with verification evidence.

**Interpretation:** `/health` initially proves process responsiveness, not database or dependency readiness.

**Gate:** Clean checkout starts, HTTP test passes locally, CI passes, and setup instructions are reproducible.

## 6. Project 1A — Saved Content API: persistence (Weeks 1–2)

### Product boundary

Store references to externally identified content, not the underlying content itself. Keep the initial content namespace singular and documented.

### Proposed HTTP contract

```http
POST   /v1/items
GET    /v1/items
GET    /v1/items/{id}
PATCH  /v1/items/{id}
DELETE /v1/items/{id}
```

| Operation / condition | Intended status |
|---|---|
| Create | `201 Created` |
| Read, list, update | `200 OK` |
| Delete | `204 No Content` |
| Missing resource | `404 Not Found` |
| Duplicate `(user_id, content_id)` | `409 Conflict` |
| Invalid request body | `422` via FastAPI/Pydantic validation |
| Unexpected server failure | `500` without disclosing internals |

### Implementation work

- [ ] Define the User and SavedItem tables with PK, FK, NOT NULL, unique constraints, and indices justified by query patterns.
- [ ] Write Alembic migrations and a deterministic development-user seed.
- [ ] Implement Pydantic request/response models, excluding caller-supplied ownership.
- [ ] Separate routing, business rules, and repository/persistence boundaries.
- [ ] Implement CRUD with explicit transaction boundaries and integrity-error handling.
- [ ] Define pagination and deterministic ordering.
- [ ] Define stable timestamp semantics and update behavior.
- [ ] Verify persistence across application restart and clean database reconstruction.

### Tests

- [ ] Create/read/update/delete success
- [ ] Missing resource and invalid input
- [ ] Duplicate save rejected for one owner
- [ ] Different owners can save the same content (database-level invariant)
- [ ] Pagination boundaries and deterministic ordering
- [ ] Failed transaction rolls back
- [ ] Migration-up from a clean database

**Gate:** An actual PostgreSQL integration test suite passes, not only mocks.

## 7. Project 1B — authentication and authorization (Week 3)

### Decision boundary

Authentication proves caller identity; authorization enforces permitted actions; ownership identifies the saved-item principal. Keep these distinct.

- [ ] Select and document a narrowly scoped authentication mechanism (ADR-004 remains open).
- [ ] Resolve the principal from verified authentication context, never a request-body `user_id`.
- [ ] Scope resource reads, updates, and deletes by both item identifier and authenticated user.
- [ ] Enforce ownership on collection/list operations too.
- [ ] Remove or fail closed on fixed-user fallback outside local development.
- [ ] Test users A and B attempting cross-user read, update, delete, and enumeration.

Policy: `401` for missing/invalid authentication, `404` for inaccessible item IDs when avoiding existence disclosure, and `403` for an explicit authenticated permission denial where disclosure is acceptable. Document precise behavior.

**Gate:** No cross-user data access or mutation in integration tests; the Project 1A grain and uniqueness invariant remain unchanged.

## 8. Project 2 — asynchronous job API (Weeks 4–6)

### Product objective

Accept a job request through HTTP, persist job state, run the work in a separate worker, and allow clients to query durable status.

```text
Client -> FastAPI -> PostgreSQL job record
                         |
                         v
                  Worker claims job
                         |
                         v
                 Result / retry state
```

### HTTP contract

- `POST /v1/jobs` returns `202 Accepted` and a durable job identifier.
- `GET /v1/jobs/{id}` returns state and relevant non-sensitive metadata.

### Required mechanics

- [ ] Document a state machine: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, with retryable attempts modeled explicitly.
- [ ] Make submission durable before reporting acceptance.
- [ ] Implement safe concurrent claims using database locking or another justified transaction protocol.
- [ ] Define leases, expiration, retries, attempt limits, and terminal failure.
- [ ] Define idempotency-key scope, retention, and conflict behavior.
- [ ] Provide structured logs and job/attempt correlation.
- [ ] Simulate crashes and restart recovery.

### Falsification cases

1. Client retries a submission after a timeout: same logical request should not create unintended duplicate work.
2. Two workers claim the same job: no simultaneous successful claim under the agreed protocol.
3. Worker crashes mid-job: expired claim is recovered safely.
4. External side effect succeeds but success persistence fails: retry must be safe through external idempotency or explicit reconciliation.
5. Database is unavailable: API cannot falsely assert durable acceptance or completion.

**Gate:** Demonstrated concurrent-claim, duplicate-request, and crash-recovery tests. Avoid claiming exactly-once delivery without evidence.

## 9. Project 3 — existing API hardening (Weeks 7–8)

Choose personally owned code or a sanitized, explicitly authorized reproduction of an existing API. **Do not publish or copy proprietary YouVersion implementations, data, credentials, or internal documentation into this portfolio.**

### Investigation workflow

1. Describe the observed failure or weakness.
2. Identify affected producer, consumer, contract, owner, and failure boundary.
3. Trace the request and persistence path.
4. Reproduce the defect and write a failing test.
5. Isolate the earliest divergence between expected and actual behavior.
6. Implement the smallest coherent correction.
7. Prove regression protection and assess adjacent failure paths.
8. Record blast radius, deployment consideration, and rollback.

Candidate cases: misleading `200` on failure, missing access check, partial-state inconsistency, unsafe retries, exception leakage, absent correlation IDs, or timeout failures.

**Gate:** A reproducible failing-before/passing-after regression test, reviewed diff, and operational explanation.

## 10. Eight-week action schedule

| Week | Primary work | Demonstrable output |
|---|---|---|
| 1 | Inspect existing scaffold, health contract, CI | First tested API PR |
| 2 | Persist saved items, migrations, transactions | CRUD and PostgreSQL tests |
| 3 | Authentication and per-user ownership | Two-user isolation tests |
| 4 | Durable job submission/state | `202` and status endpoint |
| 5 | Worker claims, retries, idempotency | Race and duplicate tests |
| 6 | Crash recovery and observability | Failure-injection evidence |
| 7 | Existing API investigation | Reproduced bug and failing test |
| 8 | Minimal correction and review | Passing regression PR + mock interview |

## 11. Weekly operating protocol

1. **Learn:** Explain a mechanism from first principles (for example, POST idempotency, transaction rollback, or worker leases).
2. **Implement:** Write a small change with an explicit contract.
3. **Verify:** Exercise a success path and a realistic failure mode.
4. **Explain:** State why the design was chosen, alternative considered, failure behavior, and evidence.
5. **Log:** Record command/test results, commit or PR, what was independently done, what was assisted, and unresolved questions.

## 12. Quality, CI, and security policy

- Prefer short-lived feature branches and reviewable changes.
- Require lint and relevant automated tests in CI before merging.
- Use reproducible installations and no committed secrets.
- Use environment-based configuration; fail closed when required configuration is missing.
- Test against actual persistence where persistence correctness is at stake.
- Do not infer runtime correctness from compilation, successful mock tests, or documentation.
- Do not expose single-user unauthenticated development mode publicly.
- Introduce containers, staging deployment, vulnerability scans, or brokers when a milestone creates a concrete reason.

## 13. Decision register and open questions

| Decision | Status | Notes |
|---|---|---|
| ADR-001: stable `(user_id, content_id)` grain | Proposed for adoption | Model ownership in Project 1A; enforce in Project 1B |
| ADR-002: trunk-based development | Proposed | Short-lived branches, PR validation |
| ADR-003: PostgreSQL persistence | Proposed | Transactions, constraints, migrations |
| ADR-004: authentication mechanism | OPEN | Choose and test before Project 1B |
| ADR-005: PostgreSQL-backed job store | Proposed | Avoid premature broker |
| ADR-006: idempotency/recovery guarantee | OPEN | Define limits around side effects |
| README roadmap alignment | OPEN | README currently associates auth with Phase 3 |
| Exact Project 3 source code | OPEN | Must be independently owned or authorized |

## 14. Evidence register and interview qualification gates

For every milestone save the PR/commit, commands or CI results, tests that prove behavior, known limits, and independent-vs-assisted contribution.

- [ ] Independently implement an HTTP endpoint.
- [ ] Explain status codes, validation, and pagination.
- [ ] Design correct keys, grain, and uniqueness.
- [ ] Run database migrations from a clean database.
- [ ] Demonstrate a rolled-back failed transaction.
- [ ] Enforce authentication and authorization.
- [ ] Prove two-user isolation.
- [ ] Implement and test durable idempotent job submission.
- [ ] Demonstrate safe concurrent worker claims.
- [ ] Recover a job after worker crash.
- [ ] Debug an unfamiliar failing test.
- [ ] Run project from clean checkout and defend a tradeoff in code review.

**Evidence levels:** Documented → Implemented → Tested → Demonstrated → Operationally validated. Do not collapse these levels.

## 15. Immediate backlog — next implementation action

- [ ] Read current `README.md`, project directories, and CI configuration.
- [ ] Align the README's phase ownership/auth wording with this syllabus.
- [ ] Choose a small first feature branch, e.g. `feature/rest-api-foundation`.
- [ ] Verify or implement the health endpoint.
- [ ] Write or strengthen the health contract test.
- [ ] Verify GitHub Actions results and fix failures.
- [ ] Open a PR with test evidence.
- [ ] Merge after required checks pass.

**Exit condition:** Three verifiable engineering case studies, at least one substantial independently implemented API, reproducible tests and environment setup, and a defensible explanation of behavior, security, failure modes, and recovery.
