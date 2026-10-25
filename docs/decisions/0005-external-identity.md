# ADR 0005: External access-token verification

Logical ID: P1-ADR-004 in [`PROJECT_1_ENGINEERING_FIDELITY.md`](../PROJECT_1_ENGINEERING_FIDELITY.md) §11; refines syllabus ADR-004 (authentication mechanism, OPEN there). Implemented by `01-rest-api/app/tokens.py`, `app/auth.py` (`current_principal`) and the `OIDC_*` settings in `app/config.py`.

## Status

Accepted (verification PR-05; principal-to-user mapping PR-06)

## Context

Spec §5 requires that a caller is identified by a token issued by a trusted external provider, never by anything the client states about itself (INV-03, F-012 to F-015). No provider has been chosen yet. This ADR therefore fixes a generic OIDC access-token profile that any mainstream provider can satisfy, and lists under **OPEN: provider checklist** what must be confirmed when one is picked.

Verification and identity mapping are separate concerns. This ADR covers verification (PR-05); mapping a verified token to a stored user is PR-06.

## Decision

1. **Verification order** (`TokenVerifier.verify`); the first failure ends the check:
   1. Parse the header without verifying. A malformed token is invalid.
   2. `alg` must be in the configured allowlist. This runs **before any key lookup**, so `alg: none` and an HS256 token keyed with the public key (algorithm confusion) are refused without touching key material. The decoder then receives only that one algorithm.
   3. `typ` must be absent, or one of `JWT`, `at+jwt`, `application/at+jwt` (case-insensitive). This keeps ID tokens (`id+jwt`) from being accepted as access tokens.
   4. `kid` must be a non-empty string. There is no "try every key" fallback.
   5. The key comes only from the configured key source. `jku`, `x5u`, `jwk` and `x5c` headers are attacker-controlled and are never read.
   6. `jwt.decode` verifies the signature, `iss`, `aud`, `exp` and `nbf` (when present), and requires `exp`, `iss`, `aud` and `sub` to exist.
   7. `sub` must be a string that is non-blank after trimming.
   8. Scopes are the union of the space-separated `scope` claim and the `scp` claim (list or single string). When `OIDC_REQUIRED_SCOPE` is set and missing, the request is refused as insufficient scope. Scope is checked last, so a forged token is always 401, never 403.
2. **Algorithm allowlist:** `OIDC_ALGORITHMS`, a subset of `RS256`, `PS256`, `ES256` (default `RS256`). HMAC algorithms and `none` fail startup.
3. **Expiry and leeway:** `exp` is mandatory. `OIDC_LEEWAY_SECONDS` (default 30, at most 120) tolerates clock skew for `exp` and `nbf` and nothing more.
4. **JWKS caching:** keys are fetched lazily on first use, held for `OIDC_JWKS_CACHE_SECONDS` (default 300, 1 to 86400) and **never served past that TTL**. The cache is a small class in this repository rather than library behavior, so these rules are explicit and testable.
5. **Refetch and failure rules:** an unknown `kid` triggers at most one refetch per 60 seconds (successful or not), so random `kid` values cannot turn this service into a load generator against the provider; a still-unknown `kid` is an invalid token. If keys are expired and cannot be refetched, or a rotation refetch fails, the answer is "keys unavailable", not "invalid token".
6. **HTTP mapping** (spec §4.5): no, malformed or non-Bearer `Authorization` header is `401 UNAUTHENTICATED`; any verification failure is `401 INVALID_TOKEN`; unavailable keys are `503 SERVICE_UNAVAILABLE`; a missing scope is `403 INSUFFICIENT_SCOPE`. Every 401 carries `WWW-Authenticate: Bearer`. Unavailable keys are not 401 because the caller did nothing wrong and a retry may succeed.
7. **No network at startup.** The service boots, and `/health` stays a liveness check, while the provider is down. Configuration errors still fail startup (F-022).
8. **The raw token never appears** in an exception message, log line or response body.
9. **Principal-to-user mapping (PR-06).** After verification, `UserRepository.resolve(issuer, subject)` returns the internal `users.id`. A SELECT runs first so routine reads do not write; a PostgreSQL upsert on `uq_external_identity` handles the first-request race. The seeded development user (`issuer` and `subject` both null) is never returned in `external_jwt` mode because verified tokens always carry non-null `iss` and `sub`.

## Consequences

### Positive

- Each documented attack (none, algorithm confusion, header-supplied keys, wrong audience or issuer, ID token as access token) has a named test.
- A provider outage degrades to 503 on authenticated routes only; it neither blocks startup nor lets unverifiable tokens through.
- A revoked or rotated-out key stops working within one cache TTL.

### Negative

- A provider outage longer than the TTL makes every authenticated request 503; there is no stale-key grace period. This is deliberate and can be revisited with evidence.
- Revocation of an individual token before `exp` is not detected (no introspection). Short token lifetimes at the provider are the mitigation.
- The `typ` and scope-claim rules are a generic profile and may need adjusting per provider.

## OPEN: provider checklist

To be settled before this ADR is Accepted, once a provider is chosen:

- [ ] **Token profile and `typ`:** does the provider issue JWT access tokens at all (some issue opaque tokens), and what `typ` header does it set?
- [ ] **Audience format:** exact `aud` value, string or list, and whether it is configurable per API.
- [ ] **Scope claim and required scope:** `scope`, `scp` or something else; which scope, if any, to require via `OIDC_REQUIRED_SCOPE`.
- [ ] **Key rotation cadence versus cache TTL:** the TTL must be shorter than the time a retired key should remain trusted, and the rotation procedure must publish new keys before signing with them.
- [ ] **Clock skew:** whether 30 seconds of leeway is enough for the provider's issuance clocks.
- [ ] **Issuer string:** the exact `iss` value, including trailing slash.

## References

- [Project 1 engineering fidelity specification](../PROJECT_1_ENGINEERING_FIDELITY.md) §4.5, §5, §8 (F-012 to F-015, F-022, F-026)
- Syllabus ADR-004 in [`API_ENGINEERING_SYLLABUS.md`](../API_ENGINEERING_SYLLABUS.md)
- [ADR 0003](0003-http-contract.md), [ADR 0008](0008-execution-boundary.md)
- RFC 8725 (JWT best current practices), RFC 9068 (JWT profile for OAuth 2.0 access tokens), RFC 6750 (Bearer tokens)
