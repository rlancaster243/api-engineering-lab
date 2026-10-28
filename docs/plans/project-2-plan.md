# Plan: Project 2 — async job API

## Context

Project 1 (`01-rest-api/`) is complete on `main`. Project 2 (`02-async-job-api/`) delivers durable background work with HTTP submission and polling (syllabus §8).

## PR sequence

| PR | Branch focus | Deliverable |
|----|----------------|-------------|
| **PR-2-00** | Foundation | **Done** ([#11](https://github.com/rlancaster243/api-engineering-lab/pull/11)): `uv.lock`, config, SQLAlchemy, Alembic, compose, CI postgres job, ADRs 0010–0011 (then Proposed, both now Accepted) |
| **PR-2-01** | Schema + submit | **Done** ([#15](https://github.com/rlancaster243/api-engineering-lab/pull/15), `dd365f2`): `jobs` table, `POST /v1/jobs` → 202 after commit, `GET /v1/jobs/{id}` |
| **PR-2-02** | Worker | **Done** ([#16](https://github.com/rlancaster243/api-engineering-lab/pull/16)): claim/lease protocol, handler registry, separate worker process, structured logs |
| **PR-2-03** | Recovery | **Done** ([#17](https://github.com/rlancaster243/api-engineering-lab/pull/17)): sweep for exhausted expired leases, reclaim probe, retry with backoff, real-crash falsification tests |
| **PR-2-04a** | Effects | **Done** ([#19](https://github.com/rlancaster243/api-engineering-lab/pull/19)): transactional outbox and reconciler, falsification case 4 |
| **PR-2-04b** | Idempotency | **Done** ([#20](https://github.com/rlancaster243/api-engineering-lab/pull/20)): `Idempotency-Key` header semantics per ADR 0011 |
| **PR-2-06** | Heartbeat | **In review** ([#22](https://github.com/rlancaster243/api-engineering-lab/pull/22)): lease heartbeat, live-lease guard on the reconciler |

`Done` in this table means merged. PR-2-06 is open, so it is marked **In review**
rather than **Done**; the row flips to `Done` with its merge commit once #22 lands.
README item 7 likewise describes PR-2-06 without a completed marker.

Project 2 is complete on `main` at the closing-docs merge `de6ffe0` (PR
[#21](https://github.com/rlancaster243/api-engineering-lab/pull/21)); the code PRs
through PR-2-04b ([#20](https://github.com/rlancaster243/api-engineering-lab/pull/20))
are all merged beneath it. PR-2-06 ([#22](https://github.com/rlancaster243/api-engineering-lab/pull/22))
adds the lease heartbeat and the reconciler's live-lease guard on top, and is in
review. Completion record:
[`docs/evidence/project-2-completion.md`](../evidence/project-2-completion.md).

## State machine

This is the implemented machine, not a target. Every edge below exists in
`app/repository.py`; the statement that owns it is named, and the guard that makes
the illegal transitions impossible is the `WHERE` clause of that same statement.

```mermaid
stateDiagram-v2
    [*] --> PENDING: POST /v1/jobs
    PENDING --> RUNNING: claim, attempt increments
    RUNNING --> SUCCEEDED: handler returns a dict
    RUNNING --> PENDING: failure with attempts left
    RUNNING --> FAILED: failure with no attempts left
    RUNNING --> RUNNING: lease expired, attempts left (reclaim)
    RUNNING --> RUNNING: handler alive, lease renewed (heartbeat)
    RUNNING --> FAILED: lease expired, no attempts left
    RUNNING --> FAILED: result cannot be stored
    SUCCEEDED --> [*]
    FAILED --> [*]
```

`SUCCEEDED` and `FAILED` are terminal; the outgoing markers mean "no statement moves a
row out of here", not a transition.

| Edge | Owning statement | What moves it |
|---|---|---|
| `[*]` → `PENDING` | `INSERT_JOB_SQL` | `POST /v1/jobs` inserts the row. `status` defaults to `PENDING`, `attempt = 0`, `next_attempt_at = now()`. (A requeue re-enters `PENDING` from `RUNNING` through `FAIL_SQL`; it is not a second initial edge.) |
| `PENDING` → `RUNNING` | `PENDING_SQL` | Claim of due `PENDING` work. Sets `attempt = attempt + 1`, `lease_owner`, `lease_expires_at = now() + LEASE_SECONDS`, `claimed_at = now()`. |
| `RUNNING` → `SUCCEEDED` | `COMPLETE_SQL` | Handler returned a `dict`. `complete_job` stores `result`, sets `finished_at`, clears the lease. |
| `RUNNING` → `PENDING` | `FAIL_SQL` | Handler raised, returned a non-dict, or had no registered type, and `attempt < max_attempts`: `last_error` set, `finished_at` NULL, `next_attempt_at = now() + backoff`, lease cleared. |
| `RUNNING` → `FAILED` | `FAIL_SQL` | The same event with `attempt == max_attempts`. One `CASE` expression picks `PENDING` or `FAILED`, which is why the two edges share a statement: a zero-row result must mean "lease lost", not "budget exhausted". |
| `RUNNING` → `RUNNING` | `RECLAIM_SQL` and `RENEW_LEASE_SQL` | Two statements serve this self-edge and they mean opposite things. `RECLAIM_SQL` is an **ownership transfer**: the lease expired and `attempt < max_attempts`, so another worker takes the row — `attempt + 1`, new `lease_owner`, new `lease_expires_at`, refreshed `claimed_at`. `RENEW_LEASE_SQL` is the **heartbeat**: the same owner pushes `lease_expires_at` forward, with `attempt` unchanged, `lease_owner` unchanged and no other column touched. It is a self-edge only because the row's state before and after is `RUNNING`; nothing about ownership or progress changes. Its `lease_owner = :worker_id` guard is what keeps the two apart — a renewal can never become a takeover, and a takeover can never be mistaken for one. |
| `RUNNING` → `FAILED` | `SWEEP_SQL` | Lease expired and `attempt >= max_attempts`: the sweep terminally fails it with `lease expired after N attempt(s); no attempts remaining`. Without this the claim's `attempt < max_attempts` guard would leave the row `RUNNING` forever. |
| `RUNNING` → `FAILED` | `COMPLETE_SQL` | The result cannot be stored (for example a non-JSON-serialisable value): `_finish` catches the completion error and issues the same statement again with `status = 'FAILED'`. Pinned by `test_non_serialisable_result_fails_job_instead_of_stranding_it`. |

`claim_next_job` runs the reclaim probe first and the pending probe second, so an
expired `RUNNING` job is never starved behind a busy `PENDING` queue.

### Illegal transitions

- **Nothing leaves `SUCCEEDED` or `FAILED`.** Every statement that changes `jobs.status`
  carries a status guard: `COMPLETE_SQL` and `FAIL_SQL` require `status = 'RUNNING'`,
  the claim probes require `PENDING` or an expired `RUNNING` lease, and `SWEEP_SQL`
  requires `RUNNING` with an expired lease. There is no statement that writes from a
  terminal state. `ck_jobs_terminal_has_finished` additionally requires `finished_at`
  on a terminal row.
- **Completion only by the current `lease_owner`.** `COMPLETE_SQL`, `FAIL_SQL` and
  `RENEW_LEASE_SQL` all bind `lease_owner = :worker_id`. A worker whose lease was
  taken over matches zero rows and logs `job.lease_lost` instead of retrying.
- **A renewal is not a transition and not a reclaim.** `RENEW_LEASE_SQL` moves only
  `lease_expires_at`, only for the current owner, only on a `RUNNING` row. It cannot
  create a lease, take one back, change `status`, change `attempt` or finish a job,
  which is why a lost lease is a dead end for the worker that lost it.
- **No `PENDING` → `SUCCEEDED` skip.** A row cannot become terminal without first
  being claimed, because `COMPLETE_SQL`'s `status = 'RUNNING'` guard is the only path
  to `SUCCEEDED`.

### Where `attempt` increments

Only at claim and reclaim time, in the shared body of `RECLAIM_SQL` and `PENDING_SQL`
(the `_CLAIM_UPDATE` template). `COMPLETE_SQL`, `FAIL_SQL`, `SWEEP_SQL` and
`RENEW_LEASE_SQL` never touch `attempt`: a heartbeat is not progress, so renewing a
lease must not spend a retry. Per
[ADR 0010](../decisions/0010-postgres-job-store.md#consequences) this
means a worker that crashes mid-job has already consumed an attempt, so a crash is
charged against the job's budget; `ck_jobs_attempt_bounds` keeps `attempt` from
exceeding `max_attempts`.

### `job_effects` is a separate lifecycle

`job_effects` rows are **not** part of the job state machine. They move `INTENT` →
`APPLIED`, driven by the handler's claim and confirmation (`CLAIM_EFFECT_SQL`,
`APPLY_EFFECT_SQL`) or by the reconciler for a stranded `INTENT`
(`PENDING_EFFECTS_SQL`, `LOCK_PENDING_EFFECT_SQL`, `APPLY_EFFECT_SQL`). The two
reconciler reads split the work: `PENDING_EFFECTS_SQL` is an unlocked candidate list
filtered by effect age against `EFFECT_RECONCILE_GRACE_SECONDS`, and
`LOCK_PENDING_EFFECT_SQL` is the decision, taken under the effect row lock, which
skips any `INTENT` row whose job still holds a live lease. No job-statement reads or
writes `job_effects.status`, so a job
can be `FAILED` with its effect `INTENT`, or `SUCCEEDED` with it `APPLIED`. Rows
cascade with the job.

## Falsification cases (syllabus)

Each case is covered by a test in the default suite; the full mapping is in the
[completion record](../evidence/project-2-completion.md#falsification-cases-and-tests).

1. Duplicate submit after timeout → no duplicate work — `test_concurrent_submissions_of_one_key_create_one_job`
2. Two workers → one winner — `test_two_concurrent_claimers_exactly_one_wins`
3. Worker crash → lease expiry recovery — `test_crashed_worker_job_is_reclaimed_by_another_worker` (real `SIGKILL`)
4. Side effect vs persistence failure → safe retry policy — `test_effect_is_applied_once_when_completion_fails` (outbox, PR #19; ADR 0011 point 4)
5. DB down on accept → no false 202 — `test_db_unavailable_returns_503_not_202`

## Verification

```bash
cd 02-async-job-api
uv sync --locked --extra dev
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/async_job_api_lab \
  REQUIRE_DATABASE=1 uv run pytest -q
```

## Open questions

- **Unauthenticated submission:** `POST /v1/jobs` requires no auth token at lab scale. Recorded as a known assumption; auth would land separately if needed.
