# Project 1 — Saved Content API
## Engineering Fidelity Specification

**Version:** 1.0  
**Date:** 2026-10-16  
**State:** Requirements finalized; implementation proposed; runtime unverified  
**Repository:** [rlancaster243/api-engineering-lab](https://github.com/rlancaster243/api-engineering-lab)  
**Project directory:** `01-rest-api/`  
**Change type:** Documentation-only; no application code, schema, or CI changes

> **Decision:** Build a production-disciplined REST API in a controlled **local + CI** environment, with PostgreSQL persistence, external identity-provider access-token verification, owner-scoped resources, and explicit failure-path testing. This specification is a target design, **not** proof that the implementation or tests exist or pass.

## 1. Requirements and scope

The five user-selected requirements are **finalized**:

| ID | Selection | Design consequence |
|---|---|---|
| REQ-01 | Generic content references | Store an opaque content ID with optional metadata; do not ingest or host source content |
| REQ-02 | Reject duplicate saves with HTTP 409 | Unique constraint is authoritative; no implicit upsert |
| REQ-03 | External identity provider | Validate provider-issued tokens; do not build password login |
| REQ-04 | Hard delete | Physically delete saved-item row, no undelete contract |
| REQ-05 | Local + CI | No public deployment, managed production auth, or standing staging environment required |

**In scope:** CRUD, PostgreSQL, Pydantic validation, Alembic migrations, ownership grain, access-token verification, authorization, deterministic pagination, transactional behavior, failure testing, CI, and engineering verification.

**Out of scope:** frontend/login UI, password and refresh-token management, content fetching, sharing, soft deletion, cache, workers, message brokers, cloud deployment, and production SLOs. Asynchronous processing belongs in Project 2.

### 1.1 Actual repository baseline [OBSERVED]

A read of `main` before this specification found:
- `01-rest-api/app/main.py`: FastAPI app and `GET /health`, returning `{"status":"ok","service":"01-rest-api"}`.
- `01-rest-api/tests/test_health.py`: HTTP TestClient status/body assertions.
- `01-rest-api/pyproject.toml`: FastAPI, Uvicorn, pytest, httpx.
- `.github/workflows/ci.yml`: pytest matrix across three project directories.
- `docs/decisions/0001-repo-layout.md`: accepted monorepo decision.
- `docs/API_ENGINEERING_SYLLABUS.md`: governing eight-week action syllabus.

**[OPEN]** No tests or CI runs were executed as evidence during the design investigation. No persistence, CRUD, or auth implementation was found among the inspected Project 1 files. Reinspect the branch before implementation, as the repository may evolve.

### 1.2 Architecture states

| State | Content |
|---|---|
| Current [OBSERVED] | Minimal FastAPI health application and test; CI workflow |
| Project 1A [PROPOSED] | Single development user, PostgreSQL schema, migration-backed saved-item CRUD |
| Project 1B [PROPOSED] | Verified external principal and enforced owner-scoped CRUD |
| Explicitly not claimed | Production deployability, passing integration tests, runtime correctness |

## 2. Target architecture and ownership

```text
                       External identity provider
                        (issuer, JWKS, audience)
                                  |
                                  | issues access token to client
                                  v
Client -- bearer token / JSON --> FastAPI HTTP adapter
                                       |
                            authentication dependency
                            (verify signature + claims)
                                       |
                            application service
                            (user principal, invariants)
                                       |
                            repository / transaction
                            (owner-scoped SQLAlchemy)
                                       |
                                  PostgreSQL
                             users / saved_items
```

| Component | Owns | Must not own |
|---|---|---|
| HTTP adapter | Routing, serialization, validation errors, HTTP response mapping | Persistence invariants |
| Authentication dependency | Trust validation, token verification, principal identification | Saved-item access rules |
| Application service | Use cases, owner scope, transactional intent | HTTP transport details |
| Repository | Parameterized, scoped SQL; persistence operations | Token trust decision |
| PostgreSQL | Foreign keys, unique constraint, durable transactions | Authentication |

**One FastAPI deployment**, separated into code modules. No microservice boundaries.

### 2.1 Governing invariants

- **INV-01:** One saved-item row represents one user's saved reference to one content ID.
- **INV-02:** `UNIQUE(user_id, content_id)`.
- **INV-03:** API client never supplies, overrides, or changes saved-item `user_id`.
- **INV-04:** Every read, list, patch, and delete uses the current internal user's scope.
- **INV-05:** Failed writes are rolled back; no false durable success is returned.
- **INV-06:** Request validation and missing-resource responses are deterministic.
- **INV-07:** Repeated/concurrent identical saves do not create duplicate logical rows.
- **INV-08:** Persistence survives process restart; migrations recreate schema on clean DB.

## 3. Persistence schema

### 3.1 Grain and key model [PROPOSED: ADR-001]

A `saved_items` row means **a particular user saved a particular content reference**. Content may be saved by many users.

- `saved_items.id` is a server-generated UUID primary key; it identifies the saved-resource API object.
- `user_id` is a non-null internal UUID foreign key.
- `content_id` is a required opaque string scoped to a single defined content namespace.
- The pair `(user_id, content_id)` is unique.
- `content_id` and `user_id` are immutable through the saved-item API.
- If multiple providers or namespaces are introduced later, redesign the uniqueness identity deliberately; do not assume an ID is globally unique.
- External identity is `(issuer, subject)`, not `subject` alone.

### 3.2 Baseline PostgreSQL DDL

This is the **target schema**, to implement through Alembic migrations rather than manual production SQL.

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY,
    issuer VARCHAR(512),
    subject VARCHAR(512),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_external_identity UNIQUE (issuer, subject),
    CONSTRAINT ck_external_identity_pair CHECK (
        (issuer IS NULL AND subject IS NULL)
        OR (issuer IS NOT NULL AND subject IS NOT NULL)
    )
);

CREATE TABLE saved_items (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    content_id VARCHAR(255) NOT NULL,
    title VARCHAR(200),
    url TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_content UNIQUE (user_id, content_id),
    CONSTRAINT ck_content_id_nonempty CHECK (length(btrim(content_id)) > 0),
    CONSTRAINT ck_title_nonempty CHECK (
        title IS NULL OR length(btrim(title)) > 0
    ),
    CONSTRAINT ck_notes_length CHECK (notes IS NULL OR length(notes) <= 2000),
    CONSTRAINT ck_updated_after_created CHECK (updated_at >= created_at)
);

CREATE INDEX ix_saved_items_user_created_id
    ON saved_items (user_id, created_at DESC, id DESC);
```

**Schema fidelity notes:**
- Generate UUIDs in the application with a cryptographically suitable generator.
- Use timezone-aware UTC timestamps and assign `updated_at` on each successful mutation.
- Normalize `content_id` consistently at the API boundary (trim surrounding whitespace; compare remaining text exactly, case-sensitive).
- The database checks are defense in depth; Pydantic provides request validation.
- The local seed user has null `issuer` and `subject` and exists only for single-user development mode. Only one such local seed identity should be configured by the lab, even though the nullable unique pair alone does not enforce singleton semantics.
- For external principals, resolve the `(issuer, subject)` pair to an internal UUID. Use an atomic conflict-safe insert/select to handle two first requests concurrently.
- `ON DELETE RESTRICT` deliberately prevents implicit cascading of saved items when a user row is deleted.

## 4. HTTP API contract

**Base:** `/v1/items`  
**Media type:** `application/json`  
**Authentication:** No public exposure in Project 1A; verified Bearer access token required in Project 1B.

| Method | Path | Success |
|---|---|---|
| POST | `/v1/items` | `201 Created`, created resource |
| GET | `/v1/items` | `200 OK`, paginated collection |
| GET | `/v1/items/{id}` | `200 OK`, resource |
| PATCH | `/v1/items/{id}` | `200 OK`, updated resource |
| DELETE | `/v1/items/{id}` | `204 No Content`, empty body |
| GET | `/health` | `200 OK`, existing process-health payload |

### 4.1 Create

```http
POST /v1/items
Content-Type: application/json

{
  "content_id": "article-042",
  "title": "Distributed systems notes",
  "url": "https://example.org/article-042",
  "notes": "Review transaction isolation"
}
```

Example successful resource (illustrative UUID/time, not runtime output):

```json
{
  "id": "c83cae64-4195-475b-9a05-96be0e52834a",
  "content_id": "article-042",
  "title": "Distributed systems notes",
  "url": "https://example.org/article-042",
  "notes": "Review transaction isolation",
  "created_at": "2026-10-16T14:00:00Z",
  "updated_at": "2026-10-16T14:00:00Z"
}
```

Resource response omits internal `user_id`. `201` must only be sent **after successful commit**.

### 4.2 Validation

| Field | Rule |
|---|---|
| `content_id` | Required, trimmed, 1–255 characters, no blank value |
| `title` | Optional, max 200 chars, if nonnull must not be blank after trim |
| `url` | Optional valid HTTP(S) URL, max 2,048 chars |
| `notes` | Optional, max 2,000 chars |
| Unknown request fields | Reject (forbid extra fields) |
| `id` | Server-generated UUID; URL identifies existing resource |
| `user_id` | Never accepted in create or patch |

Content IDs are compared case-sensitively after trimming. Submitted URLs are passive metadata: **no server-side fetching or redirects**, avoiding SSRF as a side effect of save operations.

### 4.3 PATCH

Only `title`, `url`, and `notes` are mutable:
- Omitted field: leave unchanged.
- Explicit null: clear a nullable value.
- Valid non-null: replace.
- Empty body: validation error.
- `content_id`, `user_id`, `id`: forbidden.
- Update `updated_at` on an actual successful mutation.

### 4.4 Pagination [PROPOSED: ADR-006]

Keyset pagination sorted `created_at DESC, id DESC`; default `limit=25`, maximum `limit=100`. Cursor is opaque, validated, scoped to the authenticated user, and identifies the last returned ordering key. Query records **strictly after** that key. Malformed/manipulated cursor returns `400 INVALID_CURSOR`. Cursor signing or other integrity protection is recommended; test cross-user cursor reuse. Keyset traversal does **not** promise a consistent snapshot under concurrent writes/deletes.

### 4.5 Errors and statuses

Response envelope:

```json
{
  "error": {
    "code": "ITEM_ALREADY_SAVED",
    "message": "This content is already saved.",
    "request_id": "9f53b8ab-e9d7-4c8d-b213-63d5b8213a2f"
  }
}
```

| Condition | HTTP status | Error code |
|---|---|---|
| Same user's duplicate content | 409 | `ITEM_ALREADY_SAVED` |
| Resource absent or inaccessible | 404 | `ITEM_NOT_FOUND` |
| Missing credentials | 401 | `UNAUTHENTICATED` |
| Invalid or expired credentials | 401 | `INVALID_TOKEN` |
| Invalid JSON/body values | 422 | `VALIDATION_ERROR` |
| Bad pagination cursor | 400 | `INVALID_CURSOR` |
| Database unavailable | 503 | `SERVICE_UNAVAILABLE` |
| Unexpected server error | 500 | `INTERNAL_ERROR` |

Server errors must not disclose stack traces, SQL, secrets, raw JWTs, or internal exception details. Record trace/request identifier in structured logs.

### 4.6 Hard deletion [PROPOSED: ADR-005]

`DELETE /v1/items/{id}` physically removes the owned row, returns `204` with no body. A repeat delete returns `404`; this is consistent with idempotent **intended effects** even if HTTP status differs. No restore endpoint. Deletion must be scoped atomically by item ID and owner.

## 5. Authentication and authorization [PROPOSED: ADR-004]

Project 1B is an OAuth 2.0 resource server validating access tokens issued by a configured external OpenID Connect/OAuth provider. Do not build passwords, login forms, or refresh-token issuance.

### 5.1 Verify trusted claims and signing

- Validate signature against configured issuer JWKS keys.
- Allowlist asymmetric signing algorithm(s); reject `none` and algorithm confusion.
- Verify `iss`, API `aud`, `exp`, optional `nbf`, and nonempty `sub`.
- Ensure token is an **access token suitable for the API**, not merely any JWT or an ID token.
- Configure issuer, audience, and JWKS HTTPS endpoint in trusted application settings. Never trust token-header-provided key URLs.
- Bound JWKS-cache freshness and refresh behavior; fail closed on untrusted/missing keys.
- Document clock-skew tolerance and token expiry behavior.
- Resolve `(issuer, subject)` to internal UUID. Subject alone is not globally unique.
- CI uses deterministic, locally controlled signing keys and issuer fixtures; no live identity provider or credentials required.

**[OPEN provider-specific integration]:** Provider selection, token profile, required scope/permissions, and exact JWKS strategy must be validated at PR-05. The implementation must adapt to a real provider profile instead of assuming all providers issue equivalent JWTs.

### 5.2 Resource authorization

Every collection query and item mutation is scoped to `current_user_id`. Never trust payload ownership.

Conceptual read:

```sql
SELECT * FROM saved_items
WHERE id = :item_id
  AND user_id = :current_user_id;
```

Use owner conditions on UPDATE and DELETE as well. Unauthorized caller attempting another user's item receives **the same 404** as a nonexistent record, preventing existence disclosure.

Use `401` for no/invalid authentication. Reserve `403` for explicitly forbidden actions where the policy intentionally reveals the access denial.

### 5.3 Single-user development mode safety

- Project 1A injects a fixed seeded development owner without user-supplied identifiers.
- This mode is only valid under explicit local/test configuration.
- Fail closed rather than silently reverting to fixed identity if auth settings or token verification fail.
- No public network deployment while this mode is active.

## 6. Transactions, concurrency, and recovery

### 6.1 Create workflow

1. Resolve trusted current user (seeded in 1A; verified in 1B).
2. Validate and normalize request data.
3. Begin PostgreSQL transaction.
4. Insert saved item.
5. Database uniqueness conflict → roll back → return `409`.
6. Unexpected storage error → roll back → safely map to internal/unavailable response.
7. Commit successfully.
8. Only then return `201`.

A pre-insert query cannot substitute for the uniqueness constraint: concurrent requests can both see no record. Enforce uniqueness in PostgreSQL.

### 6.2 Update/delete

- Perform ownership check and state mutation within a coherent transaction, preferably one scoped SQL statement.
- A missing or cross-owner item cannot be changed.
- On failure, restore transactional state with rollback.
- Do not report success before durable commit.
- No implied distributed exactly-once semantics.

### 6.3 POST retry behavior

Repeated POST with identical owner/content returns `409`, **not** an upsert or replay of prior success. The system prevents duplicate records, but POST is not thereby a fully idempotent HTTP contract.

## 7. Pull request implementation packages

**All PRs below are planned; none is claimed as created.**

| PR | Work | Dependencies | Exit evidence |
|---|---|---|---|
| PR-00 | Verify existing scaffold, health route/test, dependencies, CI; add linting, repair baseline issues | None | Commands documented, health test/CI green |
| PR-01 | API schemas, OpenAPI/error contract, ADRs for grain, API semantics, pagination | PR-00 | Reviewed contract and contract tests |
| PR-02 | SQLAlchemy models, Alembic, seed user, DB sessions and transactions | PR-01 | Clean DB migration, constraints and rollback tests |
| PR-03 | POST + GET by ID, uniqueness/concurrency handling, owner-scoped repository | PR-02 | 201/200/404/409 with PostgreSQL integration tests |
| PR-04 | GET list, cursor pagination, PATCH, hard DELETE, conditional writes | PR-03 | Pagination, metadata mutation, hard-delete tests |
| PR-05 | External JWT validation dependency with CI issuer/JWKS test fixture | PR-04 | Valid and forged/expired/wrong-claims token tests |
| PR-06 | Resolve internal users from trusted issuer/subject; full owner enforcement | PR-05 | Two-principal read/write/delete isolation tests |
| PR-07 | DB failure mapping, request correlation, config validation and reproducibility | PR-06 | Failure-injection suite, clean local + CI setup |
| PR-08 | Independent change/debug exercise, PR/code review evidence | PR-07 | Unaided bounded implementation and regression repair |

**Progression:** Verify before advancing. Do not treat auth middleware as complete authorization until every item route and list query has isolation tests.

## 8. Failure-case test matrix

| ID | Scenario | Expected | Priority |
|---|---|---|---|
| F-001 | Valid create | 201 and durable row | P0 |
| F-002 | Same owner saves same content twice | 409; row count unchanged | P0 |
| F-003 | Different owners save same content | Both rows permitted | P0 |
| F-004 | Concurrent duplicate create | Exactly one create; one 409 | P0 |
| F-005 | Create fails before commit | No row remains | P0 |
| F-006 | Item absent | 404 | P0 |
| F-007 | Malformed ID | Validation error | P1 |
| F-008 | Client injects user_id | Rejected | P0 |
| F-009 | Cross-user GET | 404, no metadata disclosure | P0 |
| F-010 | Cross-user PATCH | 404, no mutation | P0 |
| F-011 | Cross-user DELETE | 404, no deletion | P0 |
| F-012 | Missing/expired token | 401 | P0 |
| F-013 | Forged signature | 401 | P0 |
| F-014 | Wrong issuer/audience | 401 | P0 |
| F-015 | Unknown signing key | Fail closed | P0 |
| F-016 | Database unavailable | 503 and no false success | P0 |
| F-017 | Delete then repeat delete | 204 then 404 | P1 |
| F-018 | Empty PATCH | Validation error | P1 |
| F-019 | Equal timestamps in page order | UUID tiebreaker preserves ordering | P1 |
| F-020 | Oversized fields | Reject without write | P1 |
| F-021 | Altered pagination cursor | 400 | P1 |
| F-022 | Missing JWT verifier config | Fail startup or refuse safely | P0 |
| F-023 | Unexpected exception | 500 with sanitized response | P0 |
| F-024 | Fresh database setup | Migration and tests pass | P0 |

**P0** blocks the relevant stage's acceptance. **P1** is required before the whole Project 1 is accepted. Use actual PostgreSQL assertions for persistence properties; mocked HTTP responses alone are insufficient.

Additional targeted edge cases to validate during implementation: PATCH omitted vs null values, invalid URLs, cursor used across users, simultaneous first-time principal creation, and successful commit vs client disconnect.

## 9. CI, execution evidence, and operational boundary

Current scaffold CI runs pytest across project directories [OBSERVED], but results were not checked. Extend Project 1 CI as needed for:

1. Python lint/format checks.
2. Unit and HTTP contract tests.
3. PostgreSQL-backed integration tests in an isolated service container.
4. Alembic migration from a blank database.
5. Authentication and authorization tests after Project 1B.
6. Reproducible dependency installation with lockfile strategy.
7. Test evidence tied to the PR commit.

No public deployment is part of acceptance. Demonstrate local startup, configuration validation, log/request correlation, DB-outage response, and clean-checkout reproducibility.

**Per-PR evidence record:** commit/PR, requirements covered, passing test commands and actual results, known limitations, assistance received, and reviewer notes.

**Evidence maturity levels:** Documented → Implemented → Tested → Demonstrated → Operationally validated. Do not equate these levels.

## 10. Acceptance gates

### Gate A — Contract fidelity
- [ ] OpenAPI contracts and request/response validation match the specification.
- [ ] Grain, resource identity, and uniqueness are in approved ADRs.
- [ ] Error cases have documented status codes and machine-readable error codes.

### Gate B — Persistence correctness
- [ ] Alembic works from a fresh database.
- [ ] Concurrency cannot violate saved-item uniqueness.
- [ ] Failed writes roll back; no false success.

### Gate C — Security correctness
- [ ] Invalid, forged, and mistargeted tokens are rejected.
- [ ] Two users cannot access or mutate each other's records.
- [ ] Fixed-user shortcut cannot act as auth fallback outside local mode.

### Gate D — Operational verification
- [ ] Local and CI PostgreSQL tests pass.
- [ ] Database/configuration failure paths are exercised.
- [ ] Clean checkout reproduces app setup, migration, and tests.

### Gate E — Independent engineering qualification
- [ ] Bounded new behavior is independently implemented.
- [ ] Deliberately introduced regression is independently diagnosed and repaired.
- [ ] Tradeoffs, transaction boundaries, and failure/recovery behavior can be defended in review.

These are **target gates**, not completed results.

## 11. ADR register and naming

**[OBSERVED]** `docs/decisions/0001-repo-layout.md` already exists and is accepted. Do not overwrite its number/path.

| Logical decision | Proposed new file | Status |
|---|---|---|
| ADR-001 Stable owner/content grain | `0002-saved-item-grain.md` | Proposed |
| ADR-002 REST and duplicate behavior | `0003-http-contract.md` | Proposed |
| ADR-003 PostgreSQL + migrations | `0004-persistence.md` | Proposed |
| ADR-004 External token verification | `0005-external-identity.md` | Proposed |
| ADR-005 Hard deletion | `0006-hard-delete.md` | Proposed |
| ADR-006 Keyset pagination | `0007-pagination.md` | Proposed |
| ADR-007 Local + CI execution | `0008-execution-boundary.md` | Proposed |

The PR packages may add these as separate ADR files when the implementation work begins. This document captures their intent without falsely presenting them as accepted.

## 12. Open items and starting decision

**[OPEN]** Select an actual external provider and validate issuer, token audience, JWKS behavior, and provider-specific access-token claims at PR-05. This does not block PR-00–PR-04.

**[OPEN]** Decide exact signed cursor serialization before PR-04, consistent with owner-scoped keyset semantics.

**[OPEN]** Reconcile root README's current placement of auth within the hardening phase against the finalized Project 1B auth scope; this is a documentation follow-up, not a reason to postpone persistence.

**[OPEN]** Verify CI status and runtime behavior, rather than interpreting the existence of code or a workflow file as successful execution.

### Implementation order

Baseline verification → contracts/ADRs → schema/migrations → create/read → list/update/delete → external token validation → owner isolation → failure injection → independent engineering qualification.

**Project exit:** A tested and reproducible, owner-secured Saved Content API, backed by concrete evidence from PostgreSQL, HTTP integration tests, CI, and independently reviewed engineering changes. No claim of production deployment or formal production readiness is made.
