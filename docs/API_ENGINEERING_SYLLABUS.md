# API Engineering Lab — Action Syllabus

**Version:** 1.0  
**Status:** Active implementation plan  
**Repository:** [rlancaster243/api-engineering-lab](https://github.com/rlancaster243/api-engineering-lab)  
**Visibility:** Private  
**Primary stack:** Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy, Alembic, Pydantic, pytest, GitHub Actions  
**Planning horizon:** Eight weeks, approximately 6–8 focused hours per week

## 1. Mission and success criteria

Develop demonstrable backend/API engineering capability through implementation, verification, maintenance, and clear technical explanation. The goal is not to produce additional architecture documents alone.

The candidate should be able to independently implement a bounded endpoint, model data correctly, enforce access controls, test persistence and failure behavior, build a durable asynchronous workflow, debug an existing service, and explain engineering tradeoffs.

Prior architectural exposure to FastAPI, prior work, and prior systems is relevant context, **not proof of independent implementation ability or runtime correctness**. Record whether each exercise was independently completed or .

### Working principles

1. **Contract before implementation:** define resource identity, request/response schema, status codes, validation, failure behavior, and tests.
2. **Grain before queries:** define what a persisted row represents before creating keys and constraints.
3. **Smallest viable system:** do not introduce Kafka, Kubernetes, Redis, or a permanent staging branch without a demonstrated requirement.
4. **Failure paths count:** test invalid input, missing resources, duplication, database errors, concurrency, partial failure, and recovery when relevant.
5. **Evidence over assertions:** distinguish documented, implemented, tested, demonstrated, and operationally verified behavior.
6. **Security boundary:** keep the unauthenticated single-user API restricted to local development; never expose it as a publicly reachable service.
7. **Independent performance:** include unaided implementation and debugging exercises, and record the assistance received.

## 2. Confirmed repository baseline and intended structure

The repository has a `main` default branch, a root `README.md`, `.gitignore`, `.github/`, `docs/`, and three numbered project directories. Each directory is an independently runnable FastAPI service with its own dependencies, tests, and documentation.

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

**Branch policy:** `main` is authoritative; implement changes on short-lived `feature/*` or `fix/*` branches, validate with pull-request CI, then merge. A staging **environment** may be introduced later without adding a permanent staging **branch**.

**Documentation alignment [RESOLVED]:** Root `README.md` and `01-rest-api/README.md` have been aligned with this syllabus so ownership enforcement and authentication live in **Project 1B** (`01-rest-api/`), prior to `02-async-job-api/`, and Phase 3 (`03-api-hardening/`) focuses on defect investigation and repair.

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

**Status:** Accepted — recorded in [`docs/decisions/0002-saved-item-ownership-grain.md`](decisions/0002-saved-item-ownership-grain.md).

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

- [x] Inspect the scaffold before editing; preserve existing modules and configuration.
- [x] Verify per-module Python dependencies and reproducible installation.
- [x] Implement `GET /health` in Project 1 (or verify and test an existing implementation).
- [x] Define response `200 OK` with `{"status":"ok"}`.
- [x] Write an HTTP-level pytest test for status and response body.
- [x] Configure lint and test commands.
- [x] Make GitHub Actions run those checks on pull requests.
- [x] Create and review a bounded PR with verification evidence.

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

- [x] Define the User and SavedItem tables with PK, FK, NOT NULL, unique constraints, and indices justified by query patterns.
- [x] Write Alembic migrations and a deterministic development-user seed.
- [x] Implement Pydantic request/response models, excluding caller-supplied ownership.
- [x] Separate routing, business rules, and repository/persistence boundaries.
- [x] Implement CRUD with explicit transaction boundaries and integrity-error handling.
- [x] Define pagination and deterministic ordering.
- [x] Define stable timestamp semantics and update behavior.
- [x] Verify persistence across application restart and clean database reconstruction.

### Tests

- [x] Create/read/update/delete success
- [x] Missing resource and invalid input
- [x] Duplicate save rejected for one owner
- [x] Different owners can save the same content (database-level invariant)
- [x] Pagination boundaries and deterministic ordering
- [x] Failed transaction rolls back
- [x] Migration-up from a clean database

**Gate:** An actual PostgreSQL integration test suite passes, not only mocks.

## 7. Project 1B — authentication and authorization (Week 3)

### Decision boundary

Authentication proves caller identity; authorization enforces permitted actions; ownership identifies the saved-item principal. Keep these distinct.

- [x] Select and document a narrowly scoped authentication mechanism ([ADR 0005](decisions/0005-external-identity.md); syllabus ADR-004).
- [x] Resolve the principal from verified authentication context, never a request-body `user_id`.
- [x] Scope resource reads, updates, and deletes by both item identifier and authenticated user.
- [x] Enforce ownership on collection/list operations too.
- [x] Remove or fail closed on fixed-user fallback outside local development.
- [x] Test users A and B attempting cross-user read, update, delete, and enumeration.

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

Choose personally owned code or a sanitized, explicitly authorized reproduction of an existing API. **Do not publish or copy proprietary third-party implementations, data, credentials, or internal documentation into this portfolio.**

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
| ADR-001: stable `(user_id, content_id)` grain | Accepted (`0002-saved-item-ownership-grain.md`) | Model ownership in Project 1A; enforce in Project 1B |
| ADR-002: trunk-based development | Accepted | Short-lived branches, PR validation |
| ADR-003: PostgreSQL persistence | Accepted | [`0004-persistence.md`](decisions/0004-persistence.md) |
| ADR-004: authentication mechanism | Accepted | [`0005-external-identity.md`](decisions/0005-external-identity.md) (OIDC Bearer access tokens) |
| ADR-005: PostgreSQL-backed job store | Accepted | [`0010-postgres-job-store.md`](decisions/0010-postgres-job-store.md) (Project 2) |
| ADR-006: idempotency/recovery guarantee | Accepted | [`0011-job-idempotency.md`](decisions/0011-job-idempotency.md) (PR-2-04b) |
| README roadmap alignment | Resolved | Root README & `01-rest-api/README.md` aligned with Project 1A/1B/3 |
| Exact Project 3 source code | OPEN | Must be independently owned or authorized |

## 14. Evidence register and interview qualification gates

For every milestone save the PR/commit, commands or CI results, tests that prove behavior, known limits, and independent-vs-assisted contribution.

- [ ] Independently implement an HTTP endpoint. *(Project 1 `/v1/items`, `/ready`; evidence: PRs #3, #10.)*
- [x] Explain status codes, validation, and pagination. *(Spec §4, `test_spec_contract.py`, ADR 0003/0007.)*
- [x] Design correct keys, grain, and uniqueness. *(ADR 0002, integration tests.)*
- [x] Run database migrations from a clean database. *(Alembic tests, CI postgres jobs.)*
- [x] Demonstrate a rolled-back failed transaction. *(`test_persistence` / item tests.)*
- [x] Enforce authentication and authorization. *(PR #8, ADR 0005.)*
- [x] Prove two-user isolation. *(`test_isolation.py`.)*
- [ ] Implement and test durable idempotent job submission. *(Project 2.)*
- [ ] Demonstrate safe concurrent worker claims. *(Project 2.)*
- [ ] Recover a job after worker crash. *(Project 2.)*
- [x] Debug an unfamiliar failing test. *(Gate E evidence, PR-08.)*
- [x] Run project from clean checkout and defend a tradeoff in code review. *(README + CI; ADRs.)*

**Evidence levels:** Documented → Implemented → Tested → Demonstrated → Operationally validated. Do not collapse these levels.

## 15. Immediate backlog — next implementation action

- [x] **Project 2 PR-2-00:** foundation on `02-async-job-api/` ([#11](https://github.com/rlancaster243/api-engineering-lab/pull/11)).
- [ ] **Project 2 PR-2-01:** job schema, `POST /v1/jobs` → 202, `GET /v1/jobs/{id}`.
- [x] Read current `README.md`, project directories, and CI configuration.
- [x] Align the README's phase ownership/auth wording with this syllabus.
- [x] Choose a small first feature branch, e.g. `feature/rest-api-foundation`.
- [x] Verify or implement the health endpoint.
- [x] Write or strengthen the health contract test.
- [x] Verify GitHub Actions results and fix failures.
- [x] Open a PR with test evidence.
- [x] Merge after required checks pass.

**Exit condition:** Three verifiable engineering case studies, at least one substantial independently implemented API, reproducible tests and environment setup, and a defensible explanation of behavior, security, failure modes, and recovery.
