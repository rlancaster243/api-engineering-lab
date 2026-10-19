# ADR 0007: Keyset pagination with signed cursors

Logical ID: P1-ADR-006 in [`PROJECT_1_ENGINEERING_FIDELITY.md`](../PROJECT_1_ENGINEERING_FIDELITY.md) §11. Not related to the idempotency/recovery decision numbered ADR-006 in the syllabus. Implemented by `01-rest-api/app/pagination.py` and the `Limit` type in `01-rest-api/app/schemas.py`.

## Status

Proposed

## Context

`GET /v1/items` must return a user's saved items in a deterministic order that stays correct while the user adds and removes items. Offset pagination skips or repeats rows whenever rows are inserted or deleted ahead of the offset. The ordering columns `(created_at, id)` are indexed by `ix_saved_items_user_created_id`, and `id` breaks ties between equal timestamps.

A cursor is client-visible state. If clients can edit it, the server must either trust the edited value or reject it; trusting a client-edited position is harmless for correctness of the query (it is still owner-scoped) but makes "an altered cursor is rejected" untestable and the contract vague.

## Decision

1. **Order:** `created_at DESC, id DESC`. "After the cursor" means later in that order, expressed as the row-value comparison `(created_at, id) < (:cursor_created_at, :cursor_id)`. It is never composed from separate `<` and `>` terms.
2. **Page size:** `limit` defaults to 25, minimum 1, maximum 100. A non-integer or out-of-range value is `422 VALIDATION_ERROR` and is never clamped. The query fetches `limit + 1` rows to decide whether a next page exists.
3. **Response:** `{"items": [...], "next_cursor": "<token>" | null}`. `next_cursor` is `null` when no further row exists.
4. **Cursor format:** `base64url(payload) "." base64url(HMAC-SHA256(key, base64url(payload)))`, unpadded. The payload is compact JSON with sorted keys and exactly the fields `v` (version, 1), `u` (user id), `s` (sort tag), `t` (`created_at` as integer **microseconds** since the Unix epoch), `i` (item id, canonical lowercase UUID).
5. **Verification order:** length cap (512 characters), structure, canonical signature encoding, **signature**, then payload decoding and fields, then binding to the requesting user and the sort tag. The signature is verified before anything it covers is parsed. Any failure returns `400 INVALID_CURSOR` with the same body, so the response is not an oracle for which check failed.
6. **Key:** from the `CURSOR_SIGNING_KEY` setting, at least 32 bytes. When the list route exists (PR-04), application startup must fail if the key is missing or short. Until then no code path uses a cursor, so startup does not yet depend on it.
7. **Integer microseconds, not ISO strings or float seconds**, because PostgreSQL `timestamptz` has microsecond precision. A coarser encoding makes two rows with nearly equal timestamps indistinguishable and can skip or repeat one of them at a page boundary.
8. **Why `400` for a bad cursor but `422` for a bad `limit`:** the cursor is an opaque token the server issued, so a bad one is a protocol error; `limit` is an ordinary field a client composes.

### Consistency limit (accepted)

Traversal is **not** a snapshot. `created_at` defaults to the transaction **start** time, so a long transaction can commit a row whose `created_at` sorts before a position a client has already passed. An in-progress traversal does not return that row; a fresh traversal does. Rows deleted mid-traversal simply do not appear. This limit is accepted for Project 1 and must stay documented.

## Consequences

### Positive

- Pages are stable under concurrent inserts and deletes ahead of the cursor, and a single index serves the query.
- An edited, cross-user or cross-sort cursor is detected deterministically, so the contract is testable.
- Microsecond integers round-trip exactly with the database.

### Negative

- Rotating the signing key invalidates every outstanding cursor; clients must restart traversal. Key rotation with overlapping keys is out of scope for Project 1.
- The cursor leaks the last row's `created_at` and id to its owner. This is acceptable because the owner can read those fields on the item itself.
- The late-commit omission above is a real, if narrow, correctness gap for concurrent writers.

## Alternatives considered

- **Offset/limit:** simple, but skips and repeats rows under concurrent change and degrades on deep pages.
- **Unsigned cursor:** would work, but "tampered cursor returns 400" could not hold, because a well-formed edited cursor decodes cleanly.
- **Server-stored cursors:** removes the signing key but adds a table, expiry and cleanup for no benefit at this scale.
- **Order by `id` alone with a time-ordered UUID:** removes the transaction-start skew but ties ordering to the UUID scheme and changes the schema's index.

## References

- [Project 1 engineering fidelity specification](../PROJECT_1_ENGINEERING_FIDELITY.md) §4.4, §8 (F-019, F-021, F-032)
- [ADR 0003: Saved-item HTTP contract](0003-http-contract.md)
