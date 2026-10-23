# ADR 0006: Hard deletion of saved items

Logical ID: P1-ADR-005 in [`PROJECT_1_ENGINEERING_FIDELITY.md`](../PROJECT_1_ENGINEERING_FIDELITY.md) §11. Not related to the job-store decision numbered ADR-005 in the syllabus. Implemented by `ItemRepository.delete` in `01-rest-api/app/repository.py`.

## Status

Proposed

## Context

Requirement REQ-04 selects hard deletion. A saved item is a user's bookmark of content the service does not host; nothing else references it, and there is no audit or restore requirement.

## Decision

1. `DELETE /v1/items/{id}` physically removes the row in one owner-scoped statement and returns `204` with an empty body.
2. Deleting a missing item, an already-deleted item, or another owner's item returns `404 ITEM_NOT_FOUND`. A repeat delete is therefore `404`: the **effect** is idempotent (the row is gone), the status is not.
3. There is no restore endpoint and no tombstone.
4. Deleting a **user** with saved items is refused by `ON DELETE RESTRICT`; removing a user is a deliberate operation that must delete their items first, never an implicit cascade.

## Consequences

### Positive

- No soft-delete flag to forget in every query, and no growth of dead rows.
- Saving the same content again after deletion simply works; the unique constraint never sees a deleted row.

### Negative

- Deletion is irreversible; a mistaken delete can only be repaired by the user saving the item again, losing its notes and `created_at`.
- Clients that retry a `DELETE` after a lost response see `404`, not `204`, and must treat both as success.

## Alternatives considered

- **Soft delete (`deleted_at`):** adds undelete and audit, but every query and the unique constraint must then account for deleted rows. Not required by REQ-04.
- **`204` on repeat delete:** hides the difference between "deleted now" and "never existed or not yours"; the spec keeps `404` to match GET.

## References

- [Project 1 engineering fidelity specification](../PROJECT_1_ENGINEERING_FIDELITY.md) §1, §3.2, §4.6, §8 (F-011, F-017)
- [ADR 0004: Persistence](0004-persistence.md)
