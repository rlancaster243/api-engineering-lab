# 2026-10-25 — Project 1B auth and isolation (`01-rest-api`)

## What I did

- Landed OIDC access-token verification (PR-05) and principal-to-user resolution with two-user HTTP isolation tests (PR-06).
- Added `app/users.py`, wired `current_user_id` in `external_jwt` mode, and documented the decision in [ADR 0005](../decisions/0005-external-identity.md).

## What I learned

- Verification and user mapping are separate steps so the API stays fail-closed between merges.
- Cross-owner access returns `404` so item existence is not leaked; `503` is reserved for unavailable signing keys.

## Assistance attribution

-  implementation and documentation on branch `cursor/project-1b-jwt-auth-530c`.

## Links

- [Project 1B plan](../plans/project-1b-plan.md)
- [ADR 0005](../decisions/0005-external-identity.md)
