# Project 1 — Saved Content API
## Engineering Fidelity Specification

**Version:** 1.1 (post-review revision)  
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

A read of `main` at commit `36bc835` (Milestone 0 complete) found. This supersedes the earlier read at `ea08bd7`, which predates Milestone 0:
- `01-rest-api/app/main.py`: FastAPI app and `GET /health`, returning `{"status":"ok"}` through a `HealthResponse` model.
- `01-rest-api/tests/test_health.py`: HTTP TestClient assertions for `200` with a JSON body, and `405` for `POST /health`.
- `01-rest-api/pyproject.toml`: runtime FastAPI and Uvicorn; dev pytest, httpx and Ruff (line length 100, rules `E,F,I,UP,B`).
- `.github/workflows/ci.yml`: matrix across three project directories running `ruff check`, `ruff format --check` and pytest.
- `docs/decisions/0001-repo-layout.md` and `docs/decisions/0002-saved-item-ownership-grain.md`: accepted decisions.
- `docs/API_ENGINEERING_SYLLABUS.md`: governing eight-week action syllabus, status Active, Milestone 0 tasks checked; root and `01-rest-api` READMEs aligned with it.

**[OPEN]** No tests or CI runs were executed as evidence during the design investigation; the check runs on this PR's head are the only execution evidence, and this document does not claim they pass. No persistence, CRUD, or auth implementation exists among the inspected Project 1 files. Reinspect the branch before implementation, as the repository may evolve.

### 1.2 Architecture states

| State | Content |
|---|---|
| Current [OBSERVED] | Minimal FastAPI health application and test; Ruff + pytest CI workflow; accepted grain ADR (`0002`) |
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

### 3.1 Grain and key model [ACCEPTED: P1-ADR-001 = `docs/decisions/0002-saved-item-ownership-grain.md`]

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

-- UNIQUE (issuer, subject) treats NULLs as distinct, so it cannot constrain the
-- local seed identity. This index allows at most one row with a NULL issuer.
CREATE UNIQUE INDEX uq_users_local_seed ON users ((1)) WHERE issuer IS NULL;

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
    -- Trim set E' \t\n\r\f\x0b' (space, tab, LF, CR, FF, VT) must equal the API trim set (section 4.2).
    CONSTRAINT ck_content_id_normalized CHECK (
        length(btrim(content_id, E' \t\n\r\f\x0b')) > 0
        AND content_id = btrim(content_id, E' \t\n\r\f\x0b')
    ),
    CONSTRAINT ck_title_nonempty CHECK (
        title IS NULL OR length(btrim(title, E' \t\n\r\f\x0b')) > 0
    ),
    CONSTRAINT ck_url_shape CHECK (
        url IS NULL OR (length(url) <= 2048 AND url ~* '^https?://')
    ),
    CONSTRAINT ck_notes_length CHECK (notes IS NULL OR length(notes) <= 2000),
    CONSTRAINT ck_updated_after_created CHECK (updated_at >= created_at)
);

CREATE INDEX ix_saved_items_user_created_id
    ON saved_items (user_id, created_at DESC, id DESC);
```

**Schema fidelity notes:**
- Generate UUIDs in the application with a cryptographically suitable generator.
- **Single time source: the database.** `created_at` and `updated_at` both come from PostgreSQL `now()` (column default on INSERT, `updated_at = now()` inside the UPDATE statement on PATCH). The application never supplies timestamps, which removes app/DB clock skew as a cause of `ck_updated_after_created` violations. `now()` is transaction-start time, which matters for pagination (section 4.4). API timestamps are serialized as UTC.
- Normalize `content_id` at the API boundary: strip the section 4.2 trim set, apply Unicode NFC, then compare exactly and case-sensitively. Without NFC, canonically equivalent strings would be distinct saved items. [OPEN] PostgreSQL 13+ with a UTF8 database can add `content_id IS NFC NORMALIZED` as a DB check; decide in PR-02.
- The database checks are defense in depth and must accept exactly what the API accepts (same trim set, same length and URL limits). PR-02 includes tests that insert unnormalized values by direct SQL and expect rejection.
- The local seed user has null `issuer` and `subject`, a fixed well-known UUID (`00000000-0000-0000-0000-000000000001`), and exists only for single-user development mode. `uq_users_local_seed` enforces at most one such row; seeding is idempotent (`ON CONFLICT DO NOTHING`) and dev-mode resolution looks the user up by that UUID, never by "the row with NULL issuer".
- For external principals, resolve `(issuer, subject)` to an internal UUID with a single statement: `INSERT ... ON CONFLICT (issuer, subject) DO UPDATE SET issuer = EXCLUDED.issuer RETURNING id`. The no-op update makes the statement return the existing row, so a concurrent first request cannot get an empty result. Do not use `DO NOTHING` followed by a separate SELECT.
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
| GET | `/ready` | `200 OK` with `{"status":"ready"}` when PostgreSQL answers `SELECT 1`; `503 SERVICE_UNAVAILABLE` when not (PR-08) |

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
| `content_id` | Required, trimmed, NFC-normalized, 1–255 characters after normalization, no blank value |
| `title` | Optional, max 200 chars, if nonnull must not be blank after trim |
| `url` | Optional valid HTTP(S) URL, max 2,048 chars |
| `notes` | Optional, max 2,000 chars |
| Unknown request fields | Reject (forbid extra fields) |
| `id` | Server-generated UUID; URL identifies existing resource |
| `user_id` | Never accepted in create or patch |

**Trim set:** the API trims exactly space, tab, LF, CR, FF and VT (`str.strip(" \t\n\r\f\v")`, not Python's default Unicode-aware `strip()`), matching the database checks in section 3.2. Content IDs are compared case-sensitively after trimming and NFC normalization. Submitted URLs are passive metadata: **no server-side fetching or redirects**, avoiding SSRF as a side effect of save operations.

### 4.3 PATCH

Only `title`, `url`, and `notes` are mutable:
- Omitted field: leave unchanged. Implementations must distinguish omitted from null (Pydantic v2 `model_fields_set`, or a sentinel).
- Explicit null: clear the value. `title`, `url` and `notes` are all nullable; every resulting value must still satisfy the section 4.2 rules.
- Valid non-null: replace.
- Body with no recognized field (`{}`): `422 VALIDATION_ERROR`. A non-empty body is always valid input, even if every value equals the stored value.
- `content_id`, `user_id`, `id`: forbidden (unknown-field rejection).
- **No-op PATCH** (all supplied values equal current state): `200` with the current resource; `updated_at` is unchanged. A real change sets `updated_at = now()`.
- Implement as one owner-scoped `UPDATE ... SET updated_at = CASE WHEN (title, url, notes) IS DISTINCT FROM (:title, :url, :notes) THEN now() ELSE updated_at END ... WHERE id = :id AND user_id = :uid RETURNING *`. The row matches whenever the caller owns it, so zero returned rows always means `404`, never "nothing changed".

### 4.4 Pagination [PROPOSED: P1-ADR-006]

`GET /v1/items?limit=<int>&cursor=<opaque>`

Response:

```json
{
  "items": [ { "...": "resource as in 4.1" } ],
  "next_cursor": "opaque-string-or-null"
}
```

- Ordering: `created_at DESC, id DESC`. "After the cursor" means **later in that order**, i.e. `(created_at, id) < (:cursor_created_at, :cursor_id)` as a row-value comparison. Do not compose it from separate `<` and `>` terms.
- `limit`: default 25, minimum 1, maximum 100. A non-integer or out-of-range value is rejected with `422 VALIDATION_ERROR` (no clamping). Fetch `limit + 1` rows to decide `next_cursor`; it is `null` when no further row exists.
- Cursor: opaque, **HMAC-signed**, and bound to the authenticated user and the sort order. The signing key comes from trusted settings (startup fails if it is missing). The payload carries the last row's `created_at` at full microsecond precision (truncation skips or repeats rows with near-equal timestamps) and `id`. Any malformed, tampered, wrong-user or wrong-sort cursor returns `400 INVALID_CURSOR`. Key rotation is out of scope for Project 1.
- Why 400 for a bad cursor but 422 for a bad `limit`: the cursor is an opaque token the server issued, so a bad one is a protocol error rather than a field-validation failure. Both use the section 4.5 envelope.
- **Consistency limit:** traversal does not promise a snapshot under concurrent writes or deletes. Because `created_at` is transaction-start time, a long transaction can commit a row whose `created_at` sorts before a position the client has already passed; that row is not returned by the in-progress traversal (a later fresh traversal returns it). This is accepted for Project 1 and must appear in the ADR.

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
| Valid token lacking the required scope/permission (if the provider profile requires one) | 403 | `INSUFFICIENT_SCOPE` |
| Invalid JSON, body, query-parameter or path-parameter values (including malformed item ID and bad `limit`) | 422 | `VALIDATION_ERROR` |
| Bad pagination cursor | 400 | `INVALID_CURSOR` |
| Unknown route | 404 | `ROUTE_NOT_FOUND` |
| Wrong method for a known route | 405 | `METHOD_NOT_ALLOWED` |
| Database or signing keys unavailable | 503 | `SERVICE_UNAVAILABLE` |
| Unexpected server error | 500 | `INTERNAL_ERROR` |

**Uniform envelope is an implementation requirement, not a default.** FastAPI returns `{"detail": ...}` for validation, unknown-route and wrong-method errors, and echoes submitted input. The application must register handlers for `RequestValidationError` and Starlette `HTTPException` (which covers 404 and 405) that emit the envelope above with a `request_id`. Validation responses may include `details: [{"field", "rule"}]` but must not echo submitted values. Use `HTTPBearer(auto_error=False)` (or equivalent) so missing credentials map to `401 UNAUTHENTICATED` rather than a framework-default status. Every `401` carries `WWW-Authenticate: Bearer` (RFC 6750).

Server errors must not disclose stack traces, SQL, secrets, raw JWTs, or internal exception details. Record trace/request identifier in structured logs.

### 4.6 Hard deletion [PROPOSED: P1-ADR-005]

`DELETE /v1/items/{id}` physically removes the owned row, returns `204` with no body. A repeat delete returns `404`; this is consistent with idempotent **intended effects** even if HTTP status differs. No restore endpoint. Deletion must be scoped atomically by item ID and owner.

## 5. Authentication and authorization [PROPOSED: P1-ADR-004]

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

Use `401` for no/invalid authentication. Reserve `403` for explicitly forbidden actions where the policy intentionally reveals the access denial; the only planned use is `INSUFFICIENT_SCOPE` for a valid token that lacks the scope the provider profile requires (decided at PR-05). Cross-owner item access stays `404`.

### 5.3 Single-user development mode safety

- Project 1A injects the fixed seeded development owner (section 3.2) without user-supplied identifiers.
- Two settings control this: `AUTH_MODE` (`external_jwt` or `local_dev`) and `APP_ENV`. **The default is `external_jwt`.** `local_dev` is accepted only when `APP_ENV` is `local` or `test`; any other combination fails application startup.
- In `external_jwt` mode the seed user is unreachable: no code path may resolve it, including after a verifier or JWKS failure. Those failures return `401`/`503`, never the seed identity.
- No public network deployment while `local_dev` is active.

## 6. Transactions, concurrency, and recovery

### 6.1 Create workflow

1. Resolve trusted current user (seeded in 1A; verified in 1B).
2. Validate and normalize request data.
3. Begin PostgreSQL transaction.
4. Insert saved item.
5. Unique violation on `uq_user_content` (SQLSTATE `23505` **and** constraint name `uq_user_content`) → roll back → return `409 ITEM_ALREADY_SAVED`.
6. Any other storage error → roll back → safely map to `500`/`503`. This includes FK violations (`23503`), CHECK violations (`23514`) and a primary-key collision (`23505` on the PK constraint). They must never produce `409`, because that would tell the client the content is already saved when it is not.
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
| PR-00 | Re-verify the Milestone 0 baseline (health route/test, Ruff, CI) with fresh CI evidence; fix CI per section 9 (job-level Postgres service for `01-rest-api` only, lockfile-keyed cache); **P1-ADR-007** | None | Commands documented, health test/CI green, ADR-007 accepted |
| PR-01 | API schemas, OpenAPI/error contract including framework-error handlers, cursor format and signing; **P1-ADR-002, P1-ADR-006** (P1-ADR-001 is already accepted as `0002`) | PR-00 | Reviewed contract and contract tests, ADRs accepted |
| PR-02 | SQLAlchemy models, Alembic, seed user, DB sessions and transactions; **P1-ADR-003** | PR-01 | Clean DB migration, constraints and rollback tests, direct-SQL rejection tests, ADR-003 accepted |
| PR-03 | POST + GET by ID, uniqueness/concurrency handling, owner-scoped repository | PR-02 | 201/200/404/409 with PostgreSQL integration tests |
| PR-04 | GET list, cursor pagination, PATCH, hard DELETE, conditional writes; **P1-ADR-005** | PR-03 | Pagination, metadata mutation, hard-delete tests, ADR-005 accepted |
| PR-05 | External JWT validation dependency with CI issuer/JWKS test fixture; **P1-ADR-004** | PR-04 | Valid and forged/expired/wrong-claims token tests, ADR-004 accepted |
| PR-06 | Resolve internal users from trusted issuer/subject; full owner enforcement | PR-05 | Two-principal read/write/delete isolation tests |
| PR-07 | DB failure mapping, request correlation, config validation and reproducibility | PR-06 | Failure-injection suite, clean local + CI setup |
| PR-08 | Independent change/debug exercise, PR/code review evidence | PR-07 | `GET /ready` + Gate E evidence (`docs/evidence/project-1-gate-e-pr08.md`) |

**Delivery note [OBSERVED]:** PR-02, PR-03 and PR-04 were delivered together in GitHub PR #3, which began before this spec merged and was then brought into conformance with it. It carries P1-ADR-003 (`0004`) and P1-ADR-005 (`0006`).

**ADR ownership:** each ADR is written as Proposed and moved to Accepted in the PR listed above, as part of that PR's review. Gate A is evaluated against the ADRs owned by PR-00 to PR-01.

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
| F-007 | Malformed ID | 422 `VALIDATION_ERROR` | P1 |
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
| F-018 | Empty PATCH (`{}`) | 422 `VALIDATION_ERROR` | P1 |
| F-019 | Equal timestamps in page order | UUID tiebreaker preserves ordering | P1 |
| F-020 | Oversized fields | Reject without write | P1 |
| F-021 | Malformed, tampered, wrong-user or wrong-sort pagination cursor | 400 `INVALID_CURSOR` | P1 |
| F-022 | Missing JWT verifier config | Fail startup or refuse safely | P0 |
| F-023 | Unexpected exception | 500 with sanitized response | P0 |
| F-024 | Fresh database setup | Migration and tests pass | P0 |
| F-025 | `AUTH_MODE=local_dev` with `APP_ENV` other than `local`/`test` | Startup refuses | P0 |
| F-026 | `AUTH_MODE=external_jwt`: no token, or verifier/JWKS failure | 401/503; seed user never resolved | P0 |
| F-027 | Two concurrent first requests from the same new `(issuer, subject)` | Both succeed with the same internal user id; one `users` row | P0 |
| F-028 | FK, CHECK or PK violation during create | 500/422 path, never `409 ITEM_ALREADY_SAVED`; no row | P0 |
| F-029 | No-op PATCH (values equal stored values) | 200 with current resource; `updated_at` unchanged | P1 |
| F-030 | PATCH with omitted vs explicit-null fields | Omitted unchanged; null clears | P1 |
| F-031 | Unknown route; wrong method on known route | 404 `ROUTE_NOT_FOUND` / 405 `METHOD_NOT_ALLOWED`, envelope with `request_id` | P1 |
| F-032 | `limit` of 0, 101 or non-integer | 422 `VALIDATION_ERROR` | P1 |
| F-033 | Direct SQL insert of tab-only or untrimmed `content_id`, over-long or non-HTTP(S) `url` | Rejected by DB checks | P1 |
| F-034 | Second seed-user row (NULL issuer) inserted | Rejected by `uq_users_local_seed` | P1 |

**P0** blocks the relevant stage's acceptance. **P1** is required before the whole Project 1 is accepted. Use actual PostgreSQL assertions for persistence properties; mocked HTTP responses alone are insufficient.

Additional targeted edge cases to validate during implementation: invalid URLs, NFC-equivalent `content_id` values, and successful commit vs client disconnect.

## 9. CI, execution evidence, and operational boundary

Current CI runs `ruff check`, `ruff format --check` and pytest across the three project directories [OBSERVED at `36bc835`], but results were not checked by this document. Three constraints in `.github/workflows/ci.yml` [OBSERVED] must be fixed in PR-00 before the list below is achievable:

- The commented `services:` stub after the pytest step sits under `steps`. `services` is a **job-level** key, so uncommenting it in place fails workflow validation.
- A `services:` block on the shared matrix job would start PostgreSQL for projects 02 and 03, which have no DB tests. Put the Postgres service on a dedicated `01-rest-api` job (or gate it per matrix entry).
- `cache-dependency-path` points at `pyproject.toml` only, which cannot support the lockfile strategy in item 6. Key the cache on the lockfile once one exists.

Extend Project 1 CI as needed for:

1. Python lint/format checks (Ruff, already present).
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
- [x] OpenAPI contracts and request/response validation match the specification.
- [x] Grain, resource identity, and uniqueness are in approved ADRs.
- [x] Error cases have documented status codes and machine-readable error codes.
- [x] Every error response, including framework-generated validation, 404 and 405 errors, uses the section 4.5 envelope with `request_id` (F-007, F-018, F-031, F-032).

### Gate B — Persistence correctness
- [x] Alembic works from a fresh database.
- [x] Concurrency cannot violate saved-item uniqueness.
- [x] Failed writes roll back; no false success.

### Gate C — Security correctness
- [x] Invalid, forged, and mistargeted tokens are rejected.
- [x] Two users cannot access or mutate each other's records.
- [x] Fixed-user shortcut cannot act as auth fallback outside local mode (F-025, F-026).
- [x] Concurrent first-time principal creation yields one user (F-027) (`test_concurrent_resolve_creates_one_user_row`, which holds all eight resolvers at the INSERT with a barrier).

### Gate D — Operational verification
- [x] Local and CI PostgreSQL tests pass.
- [x] Database/configuration failure paths are exercised (`test_ready_returns_503_against_a_real_unreachable_database`, `test_ready_executes_select_1_on_the_apps_engine`, `test_non_duplicate_integrity_error_is_a_sanitized_500_not_503`, `test_generic_non_unavailable_dbapi_error_is_a_sanitized_500`).
- [x] Clean checkout reproduces app setup, migration, and tests.

### Gate E — Independent engineering qualification
- [x] Bounded new behavior is independently implemented (`GET /ready`, PR-08; evidence in `docs/evidence/project-1-gate-e-pr08.md`).
- [x] Deliberately introduced regression is independently diagnosed and repaired (documented false-positive readiness probe; tests `test_ready_returns_503_against_a_real_unreachable_database` and `test_ready_executes_select_1_on_the_apps_engine`).
- [x] Tradeoffs, transaction boundaries, and failure/recovery behavior can be defended in review (ADR 0009, Gate E evidence doc).

The repository owner keeps Gate E ticked (decision 2026-10-26). PR-08 was implemented with AI assistance (see `docs/evidence/project-1-gate-e-pr08.md`), so Gate E records reviewed delivery and defense of the design, not unaided implementation. The Project 1 audit found the original regression guard tautological: it wrapped the real probe and only counted calls, so removing the probe left it green. The audit follow-up replaced it with tests that fail when the probe is removed. An unaided repeat remains the stronger qualification and is not claimed.

## 11. ADR register and naming

**[OBSERVED]** `docs/decisions/0001-repo-layout.md` and `docs/decisions/0002-saved-item-ownership-grain.md` already exist on `main` and are accepted. Do not overwrite their numbers/paths; new ADRs start at `0003`.

**Naming and precedence.** This document's decisions use the `P1-ADR-NNN` prefix. A bare `ADR-NNN` refers to the decision register in `docs/API_ENGINEERING_SYLLABUS.md` (§13), where ADR-002 is trunk-based development, ADR-005 is the PostgreSQL job store and ADR-006 is the idempotency/recovery guarantee. Those do **not** mean what an unprefixed use of the same number might suggest here. In PRs and commit messages, cite the ADR **file number** (`0002`...`0008`), which is unique. Where the two registers describe the same decision (grain, persistence, authentication), the syllabus entry is the parent and the P1 ADR is the detailed record.

| Logical decision | Syllabus register | Proposed new file | Owning PR | Status |
|---|---|---|---|---|
| P1-ADR-001 Stable owner/content grain | ADR-001 (same decision) | `0002-saved-item-ownership-grain.md` [OBSERVED] | done (`36bc835`) | Accepted |
| P1-ADR-002 REST and duplicate behavior | none | `0003-http-contract.md` | PR-01 | Accepted |
| P1-ADR-003 PostgreSQL + migrations | ADR-003 (same decision) | `0004-persistence.md` | PR-02 | Accepted |
| P1-ADR-004 External token verification | ADR-004 (resolves its OPEN status) | `0005-external-identity.md` | PR-05 | Accepted |
| P1-ADR-005 Hard deletion | none (not syllabus ADR-005) | `0006-hard-delete.md` | PR-04 | Accepted |
| P1-ADR-006 Keyset pagination | none (not syllabus ADR-006) | `0007-pagination.md` | PR-01 | Accepted |
| P1-ADR-007 Local + CI execution | none | `0008-execution-boundary.md` | PR-00 | Accepted |

**Refinement of the accepted grain ADR [RESOLVED in PR-01].** The grain, key and `UNIQUE(user_id, content_id)` invariant in `0002` are unchanged by this spec. This spec does refine its attribute table: `id` is fixed to UUID (the ADR allows "UUID or bigint"), and `title` and `url` are added (the ADR lists only `notes`). The refinement is recorded in P1-ADR-002 (`0003-http-contract.md`); `0002` is left as accepted.

The ADR files in `docs/decisions/` are the records of each decision; this table is the index. ADRs 0003–0008 are Accepted.

## 12. Open items and starting decision

**[OPEN]** Select an actual external provider and validate issuer, token audience, JWKS behavior, and provider-specific access-token claims at PR-05. This does not block PR-00–PR-04.

**[RESOLVED in PR-01]** Signed cursor serialization (field encoding, HMAC construction, settings name `CURSOR_SIGNING_KEY`) is specified in P1-ADR-006 (`0007-pagination.md`) and implemented in `01-rest-api/app/pagination.py`. Wiring the startup-fails-without-a-key check into the application remains for PR-04, where the list route first uses a cursor.

**[RESOLVED]** README phase alignment: the root README and `01-rest-api/README.md` were aligned with the Project 1B auth scope in `36bc835` (syllabus §2).

**[OPEN]** Verify CI status and runtime behavior, rather than interpreting the existence of code or a workflow file as successful execution.

### Implementation order

Baseline verification → contracts/ADRs → schema/migrations → create/read → list/update/delete → external token validation → owner isolation → failure injection → independent engineering qualification.

**Project exit:** A tested and reproducible, owner-secured Saved Content API, backed by concrete evidence from PostgreSQL, HTTP integration tests, CI, and independently reviewed engineering changes. No claim of production deployment or formal production readiness is made.
