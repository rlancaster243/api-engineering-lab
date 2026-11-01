# Project 2 completion record

**Repository:** `02-async-job-api/` — async job API
**Status:** Complete on `main` at `e5b3c84`, which merges PR-2-06 ([#22](https://github.com/rlancaster243/api-engineering-lab/pull/22)) — the lease heartbeat and the reconciler's live-lease guard. All five syllabus §8 falsification cases are covered by tests that run in the default suite.

## Merged pull requests

| PR | Scope |
|----|-------|
| [#11](https://github.com/rlancaster243/api-engineering-lab/pull/11) | Foundation: `uv.lock`, settings, SQLAlchemy, Alembic, Compose, CI postgres job, ADRs 0010–0011 filed as Proposed |
| [#15](https://github.com/rlancaster243/api-engineering-lab/pull/15) | `jobs` table, `POST /v1/jobs` → `202` after commit, `GET /v1/jobs/{id}` (`dd365f2`) |
| [#16](https://github.com/rlancaster243/api-engineering-lab/pull/16) | Claim/lease protocol on `FOR UPDATE SKIP LOCKED`, handler registry, separate worker process, structured logs |
| [#17](https://github.com/rlancaster243/api-engineering-lab/pull/17) | Retries with capped exponential backoff, lease-expiry sweep, reclaim-first probe, real-crash falsification tests |
| [#18](https://github.com/rlancaster243/api-engineering-lab/pull/18) | CI fix: run checks on every pull request, not only those based on `main` (the stacked PRs were otherwise unchecked) |
| [#19](https://github.com/rlancaster243/api-engineering-lab/pull/19) | Transactional outbox plus reconciler, so a reclaimed job does not repeat an external side effect (falsification case 4) |
| [#20](https://github.com/rlancaster243/api-engineering-lab/pull/20) | `Idempotency-Key` on `POST /v1/jobs`: replay, `409` conflict, 255-character cap (ADR 0011) |
| [#22](https://github.com/rlancaster243/api-engineering-lab/pull/22) | Lease heartbeat (owner-scoped `RENEW_LEASE_SQL`) and the reconciler's live-lease guard, so a live-lease handler's `INTENT` row is never reconciled underneath it (`e5b3c84`) |

## ADRs accepted

| ADR | Decision | Accepted in |
|-----|----------|-------------|
| [0008](../decisions/0008-execution-boundary.md) | Project 1 execution boundary (local + CI only, no deployed environment) | PR #4 (Project 1) |
| [0010](../decisions/0010-postgres-job-store.md) | PostgreSQL-backed job store: row-per-job, `FOR UPDATE SKIP LOCKED` claim, attempts consumed at claim time | PR #16 |
| [0011](../decisions/0011-job-idempotency.md) | Job submission idempotency: key scope, retention, replay/conflict split | PR #20 |

ADR 0008 is a Project 1 decision listed here because it is the execution boundary this
project also runs under. Both Project 2 ADRs were filed as **Proposed** in PR #11 and
moved to **Accepted** by the PR that landed the implementing code, on owner
authorization recorded in the ADR body.

## Falsification cases and tests

Every test below is in `02-async-job-api/tests/` and runs in the default suite with a
real PostgreSQL database.

| # | Syllabus case | Test | What it actually does |
|---|---------------|------|-----------------------|
| 1 | Client retries a submission after a timeout → no unintended duplicate work | `test_concurrent_submissions_of_one_key_create_one_job` (`test_idempotency.py`) | Eight threads released together by a barrier submit the same `Idempotency-Key`; asserts one job id, one row, and exactly one response without `Idempotency-Replayed` |
| 2 | Two workers claim the same job → one winner | `test_two_concurrent_claimers_exactly_one_wins` (`test_claims.py`) | 20 trials of two independent connections racing for one row; exactly one claim wins and `attempt` is 1 |
| 3 | Worker crashes mid-job → expired claim recovered safely | `test_crashed_worker_job_is_reclaimed_by_another_worker` (`test_recovery.py`) | Real worker subprocess is `SIGKILL`ed inside the handler; the row stays `RUNNING` on its dead lease, then a second worker reclaims it to `SUCCEEDED` at `attempt == 2` |
| 4 | External side effect succeeds but persistence of success fails → retry is safe | `test_effect_is_applied_once_when_completion_fails` (`test_effects.py`) | The handler claims, applies and confirms the effect, then job completion is forced to raise; the row stays `RUNNING`, the next cycle reclaims it at `attempt == 2`, the handler re-enters and reports `{"skipped": True}` — the external call stays at one application |
| 5 | Database unavailable → API cannot falsely assert acceptance | `test_db_unavailable_returns_503_not_202` (`test_jobs_api.py`) | App pointed at a dead port; `POST /v1/jobs` returns `503 SERVICE_UNAVAILABLE` and the row count is unchanged |

Case 4 has a second half — a crash between claiming the effect and confirming it —
covered by `test_crash_between_claim_and_apply_leaves_the_effect_intent`,
`test_reconciler_applies_a_stranded_intent` and
`test_reclaimed_job_reconciles_an_effect_the_crash_left_intent`.

Case 1 is only the *submission* leg. A job that is reclaimed and re-run still enters
its handler again; that is case 4's problem, and the two mechanisms are independent.

## Verification record

Commands, run in `02-async-job-api/` on `main` at `e5b3c84`, the merge of PR-2-06:

```bash
cd 02-async-job-api
export PATH="$HOME/.local/bin:$PATH"
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/async_job_api_lab \
  REQUIRE_DATABASE=1 uv run pytest -q
```

Actual results:

```text
All checks passed!
35 files already formatted
209 passed, 1 warning in 14.62s
```

`REQUIRE_DATABASE=1` makes a skipped database test a failure, so **0 skipped** is
enforced by the run rather than observed by eye. The single warning is a
`StarletteDeprecationWarning` about `httpx` from a FastAPI test-client import; it is
not from this project's code.

The count moves with each PR: 193 on `main` at `f016f30`, 209 from `e5b3c84` onward.
The `209` was re-confirmed by the `postgres integration — 02-async-job-api` job on the
current `main` head. Migrations run forward from an empty database through `0001` →
`0006_job_idempotency` (`test_migration_upgrade_from_clean_database`,
`test_migration_round_trip`); PR-2-06 adds no migration.

## Known limitations

- **At-least-once, not exactly-once, for side effects.** The outbox makes a duplicate
  detectable and skippable, and the reconciler covers a crash before confirmation, but
  a third-party call must still be idempotent — normally by passing `effect_key` as
  the vendor's idempotency key.
- **A handler that loses its lease and then applies can still be double-applied.** The
  reconciler's live-lease check protects a handler that still *holds* its lease: a
  live lease means its `INTENT` row is skipped. The gap is a handler that loses its
  lease and *then* applies — a zombie. Its job no longer holds a lease, so the
  reconciler is entitled to act on the row, and both can apply. The heartbeat makes
  this rarer, because losing a lease now requires a takeover rather than merely
  outliving `LEASE_SECONDS`, but it does not make it impossible. Vendor calls must
  stay idempotent.
- **A hung handler now holds its job forever.** The heartbeat renews a running
  handler's lease indefinitely, so a handler that never returns is never reclaimed,
  never swept, and that worker is stuck on it permanently. `LEASE_SECONDS` no longer
  bounds handler runtime at all — it bounds only how long a worker may go silent
  without renewing. The heartbeat therefore trades "long handlers get reclaimed" for
  "hung handlers are never recovered". Nothing in this slice bounds handler duration;
  a max-handler-duration setting or a cap on the number of renewals would be the
  honest fix, and neither is implemented.
- **A handler cannot be stopped once it loses its lease.** Renewal detects the loss
  and records it, but the handler runs to completion because nothing safe can abort a
  call already in flight. Its work is discarded, not rolled back: an effect it
  already applied stays applied, even though its completion write was refused.
- **Recovery and reconciliation are worker-driven.** The sweep, the reconciler and the
  claim all run inside each worker cycle. If no worker runs, nothing is recovered,
  terminated or reconciled.
- **`Idempotency-Key` is unauthenticated.** There is no auth in this slice, so a `409`
  reveals whether a key exists, and two clients sending the same string address the
  same job. A key must not be a secret.
- **The fingerprint covers `type`, `payload` and `max_attempts` only.** Any other
  request field is outside the equality check.
- **`jobs.result` has no `jsonb_typeof(result) = 'object'` constraint.** The worker
  refuses to store a non-object and `GET` serves one as `null`, but the guarantee is
  in code, not in the schema; closing it needs a migration past `0006_job_idempotency`.
- **One effect per `(job_id, effect_key)`.** A genuinely repeatable effect — a
  per-attempt notification — needs an `effect_key` that includes the attempt, which
  this slice does not model.
- **No list endpoint and no auth.** Only `echo` and `notify` handlers exist.
- **The logs carry no payload, result, handler error message or traceback.** Worker
  failures are recorded as `error_class` plus `last_error_chars` (the length of the
  stored message), and the HTTP error handlers log stack frames and SQLSTATE but never
  an exception's message, because psycopg's `DETAIL:` line quotes the submitted row.
  A failure is therefore diagnosable by call site, not by value.

This is a lab system. It is not production-ready, and it does not claim exactly-once
delivery.
