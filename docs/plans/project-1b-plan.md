# Plan: Project 1B — verified principals and owner enforcement (spec PR-05, PR-06)

## Status

**Complete on `main`.** PR-05 and PR-06 merged in [#8](https://github.com/rlancaster243/api-engineering-lab/pull/8). Draft [#7](https://github.com/rlancaster243/api-engineering-lab/pull/7) on `feature/1b-token-verification` was closed without merge (interim PR-05-only branch). This file is kept as the implementation record.

## Context

Project 1A is closed on `main` (`a83c8a4`). The API works for a single seeded development owner in `AUTH_MODE=local_dev`; in the default `external_jwt` mode every item request returns `401 UNAUTHENTICATED` because no verifier exists (`01-rest-api/app/auth.py`). Phase 1B makes `external_jwt` real: verify provider-issued access tokens (PR-05), then map each verified `(issuer, subject)` to an internal user and prove two users cannot see or change each other's data (PR-06). Exit = **Gate C** in spec §10.

Decisions taken: scope is PR-05 + PR-06 only (PR-07 gets its own plan); no provider is chosen yet; implement a **generic OIDC access-token profile** and record what must be validated when a real provider is picked.

Working model (`CLAUDE.md`): I write briefs and review; a **Sonnet** subagent implements each PR; escalate to Opus only if review fails twice. Each PR is a draft, merged only on your word.

## PR-05 — access-token verification (delivered in #8; historical branch `feature/1b-token-verification`)

**Scope:** a verifier and its configuration, fully tested. Item routes stay `401` in `external_jwt` mode until PR-06 wires resolution in, so behavior on `main` stays fail-closed between the two merges.

1. **Dependency:** add `PyJWT[crypto]` to `01-rest-api/pyproject.toml`, `uv lock`. (Mature, has `PyJWKClient`; `cryptography` also lets tests mint keys.)
2. **Settings** (`app/config.py`, extend `load_settings`; required only when `AUTH_MODE=external_jwt`, else `ConfigError` = F-022):
   - `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL` (must be `https://`)
   - `OIDC_ALGORITHMS` (default `RS256`; allowlist subset of `RS256, PS256, ES256`; any `HS*` or `none` is a startup error)
   - `OIDC_LEEWAY_SECONDS` (default 30, max 120), `OIDC_JWKS_CACHE_SECONDS` (default 300)
   - `OIDC_REQUIRED_SCOPE` (optional; when set, the `scope` (space-separated) or `scp` (list) claim must contain it, else `403 INSUFFICIENT_SCOPE`)
3. **New `app/tokens.py`:**
   - `KeySource` protocol: `signing_key(kid) -> key`. Production `JwksKeySource` wraps `PyJWKClient` with a bounded cache: keys are fresh for the cache TTL; an unknown `kid` triggers at most one refetch per 60 s; a fetch failure or expired cache raises `KeysUnavailable` (never serves keys past TTL).
   - `TokenVerifier.verify(raw) -> Principal(issuer, subject, scopes)`. Order: parse header unverified → `alg` in allowlist (rejects `none` and HS confusion before any key lookup) → `kid` required → key from `KeySource` only (ignore `jku`/`x5u`/`jwk` headers) → `jwt.decode` with `algorithms=allowlist`, `audience`, `issuer`, `leeway`, `require=["exp","iss","aud","sub"]`, `nbf` honored → `sub` non-empty string → `typ` header absent, `JWT` or `at+jwt` → scope check.
   - Errors: `InvalidToken` and `KeysUnavailable`; no token text in messages or logs.
4. **`app/auth.py`:** add `current_principal` (reads `Authorization: Bearer`, no or malformed header → `401 UNAUTHENTICATED`, `InvalidToken` → `401 INVALID_TOKEN`, `KeysUnavailable` → `503 SERVICE_UNAVAILABLE`, scope miss → `403 INSUFFICIENT_SCOPE`). Reuse `ApiError`/`ErrorCode` from `app/errors.py` (codes already exist, and every 401 already gets `WWW-Authenticate: Bearer`). `current_user_id` unchanged in this PR.
5. **`app/main.py` lifespan:** build `TokenVerifier` into `app.state.token_verifier` when `external_jwt`; no network call at startup.
6. **Tests** (`tests/test_tokens.py`, fixtures in `tests/conftest.py`): an RSA and an EC key pair generated per session; a `StaticKeySource`; a helper to mint tokens. No network, no real IdP.
   - F-012 missing/expired, F-013 forged signature, F-014 wrong `iss`/`aud`, F-015 unknown `kid`, F-022 missing or unsafe config.
   - `alg: none`, HS256 signed with the public key (algorithm confusion), `alg` not in allowlist, missing `sub`/empty `sub`, future `nbf`, expiry within/beyond leeway, `jku` header pointing elsewhere is ignored, wrong `typ`, scope present/absent/`scp` list.
   - `JwksKeySource`: monkeypatched fetch for TTL expiry, refetch on unknown `kid`, rate-limited refetch, fetch failure → `KeysUnavailable`.
   - Dependency over HTTP via a test route: 401/401/403/503 mapping and `WWW-Authenticate`.
7. **Docs:** ADR `docs/decisions/0005-external-identity.md` (P1-ADR-004, **Proposed**) with an **OPEN provider checklist** (token profile, `typ`, audience form, scope claim, key rotation cadence). Spec §4.5 table row "Database unavailable" becomes "Database or signing keys unavailable" (same 503 code, so `tests/test_spec_contract.py` still passes). README and `.env.example`: the `OIDC_*` settings.

## PR-06 — principal resolution and isolation (branch `feature/1b-principal-resolution`, after PR-05 merges)

1. **New `app/users.py` `UserRepository.resolve(issuer, subject) -> UUID`:** `SELECT id` first; on miss, `INSERT ... ON CONFLICT (issuer, subject) DO UPDATE SET issuer = EXCLUDED.issuer RETURNING id`, then commit. The SELECT-first step keeps every authenticated GET from writing a row and taking a row lock; the upsert still makes the first-request race safe. **This deviates from the spec §3.2 note ("single statement")**, so the spec note is updated in this PR and ADR 0005 records why.
2. **`app/auth.py` `current_user_id`:** `local_dev` → `DEV_USER_ID` (unchanged); `external_jwt` → `current_principal` → `resolve`. No branch can return `DEV_USER_ID` in `external_jwt` (issuer is always non-null for a verified token, and the seed row has a null issuer).
3. **No changes needed** to `app/repository.py`, `app/service.py`, or the cursor: they are already owner-scoped by `user_id` (`ItemService(session, codec, user_id)`), and the cursor already binds to `user_id`.
4. **Tests** (`tests/test_isolation.py`; `api_jwt` fixture = real app in `external_jwt` with `StaticKeySource` injected into `app.state`):
   - Two principals A and B: cross-user GET/PATCH/DELETE are 404 with no mutation (F-009 to F-011 over HTTP), lists are disjoint, and A's cursor used by B is `400 INVALID_CURSOR`.
   - F-027: 8 threads resolve the same new `(issuer, subject)` concurrently → one `users` row, one id.
   - Same `sub` under two issuers → two users; seed user never resolved (row count and ownership checks); keys unavailable → 503 and nothing written (F-026).
   - First request creates the user; repeat requests do not write (row `xmin` unchanged).
5. **Docs:** spec §8 rows F-009 to F-011, F-027 covered over HTTP; delivery note in spec §7; Gate C items checked only after CI is green.

## Review protocol (applied by me to each subagent result)

- Read the full diff against this plan and spec §5. Run `ruff check`, `ruff format --check`, and `pytest -q` with `TEST_DATABASE_URL` and `REQUIRE_DATABASE=1` against a throwaway PostgreSQL 16, plus the no-database run.
- **Mutation check.** Each of these must turn a test red:
  - **PR-05:** accept `alg: none`; accept HS256 with the public key; drop the `aud` or `iss` check; huge leeway; drop `require=["exp"]`; pick the first key regardless of `kid`; honor `jku`; map `KeysUnavailable` to 401; serve keys past TTL; skip the scope check.
  - **PR-06:** resolve by `subject` only; use `DO NOTHING` + SELECT; fall back to `DEV_USER_ID` on any error; drop `user_id` from the cursor check.
- Anything that survives goes back to the subagent with the failing mutation named.

## Verification (end to end)

- CI on each PR: all four jobs green, including `postgres integration — 01-rest-api`. I read the job log to confirm the token and isolation tests actually ran.
- After PR-06, the `api_jwt` isolation tests are the end-to-end check: real app startup in `external_jwt`, real PostgreSQL, real verifier, with only the key source swapped for local keys. Running against a real provider waits for the provider decision.
- Gate C (spec §10) checked against the passing tests by ID.

## Open items to raise with you during the work

- ADR 0005 external-provider checklist remains open; ADRs 0003–0009 are Accepted on `main`.
- Real provider choice, and whether a scope is required, before any deployment beyond local + CI.
- SELECT-first resolution (PR-06, step 1): approved with "go"; the spec note is updated in PR-06.

## First actions after approval

1. ~~Commit this plan~~ — on `main` since the 1B work shipped.
2. ~~Open PR-05/PR-06~~ — completed via #8.
