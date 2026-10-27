# 02 — Async job API

## Progression

1. **PR-2-00 (Foundation):** Done ([#11](https://github.com/rlancaster243/api-engineering-lab/pull/11)) — install, `DATABASE_URL`, Alembic, `GET /health`, CI postgres job.
2. **PR-2-01 (Schema + submit):** Done (`dd365f2`) — `jobs` table (ADR 0010), `POST /v1/jobs` → 202 after commit, `GET /v1/jobs/{id}`.
3. **PR-2-02 (Worker):** Done ([#16](https://github.com/rlancaster243/api-engineering-lab/pull/16)) — claim/lease protocol, handler registry, separate worker process.
4. **PR-2-03 (Recovery):** In review — retries with backoff, lease-expiry recovery, crash falsification tests.
5. **PR-2-04:** Idempotency — `Idempotency-Key` per ADR 0011.

## Run locally

```bash
docker compose up -d
cp .env.example .env
set -a && . ./.env && set +a
uv sync --locked --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8002
curl http://127.0.0.1:8002/health

# Submit a job
curl -s -X POST http://127.0.0.1:8002/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"type": "echo", "payload": {"msg": "hello"}}' | jq .

# Poll status (replace <id> with the returned id)
curl -s http://127.0.0.1:8002/v1/jobs/<id> | jq .
```

Postgres is published on host port **5433** to avoid clashing with Project 1 on 5432.

## Run the worker

The worker is a **separate process** from the API (ADR 0010). It shares
`DATABASE_URL` and claims jobs one at a time, reclaiming an expired `RUNNING` job
before it takes fresh `PENDING` work:

```bash
# Same environment as the API, then:
uv run python -m app.worker
```

Run as many as you like against the same database — concurrency is across
processes, not within one. Claims use `FOR UPDATE SKIP LOCKED`, so two workers
never claim the same job.

Stop it with `SIGTERM` or `SIGINT` (Ctrl-C): it finishes the job in hand and exits;
it never abandons a claimed job.

Each event is one JSON object on stdout:

```json
{"ts": "2026-10-27T18:40:00.123456+00:00", "level": "info", "event": "job.claimed", "worker_id": "host-1234-ab12cd34", "job_id": "...", "job_type": "echo", "attempt": 1, "lease_expires_at": "2026-10-27T18:41:00.123456+00:00", "message": "job.claimed"}
```

Events: `worker.started`, `worker.stopped`, `job.claimed`, `job.recovered`,
`job.succeeded`, `job.failed`, `job.retrying`, `job.lease_lost`,
`worker.cycle_failed`. `job.recovered` marks a job taken over from a worker whose
lease expired; the accompanying `job.claimed` for the same row follows it.
`worker.cycle_failed` is the containment event for a cycle that raised anywhere —
the worker logs it and polls again instead of exiting.

No payload, no result and no handler message is logged. A failure event carries
`error_class` and `last_error_chars`; the message itself lives on the job row and is
served by `GET /v1/jobs/{id}`. No traceback is logged either: a formatted traceback
repeats the exception message and source locals, and a claim failure can surface a
row value out of the driver.

## Retries and crash recovery

Every worker cycle runs a **sweep** before claiming:

1. Expired `RUNNING` jobs with no attempts left are marked `FAILED` with a readable
   `last_error` and `finished_at`. This is what stops a crashed job on its last
   attempt from sitting in `RUNNING` forever.
2. Then the claim runs, **reclaim first**: an expired `RUNNING` job with attempts
   remaining is taken over before any `PENDING` work, so recovery cannot be starved
   by a busy queue.
3. Then due `PENDING` jobs, oldest first.

A handler failure is recorded in **one** statement that returns either `PENDING`
(requeue, attempts remain) or `FAILED` (attempts exhausted). Requeued jobs get
`next_attempt_at = now() + backoff`, where backoff is exponential and capped:

```
delay = min(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_RETRY_BACKOFF_SECONDS)
```

Without that deferral a job that always raises would burn every attempt in
milliseconds and starve everything behind it. Succeeding is always terminal, whatever
attempts remain.

Handlers must finish inside `LEASE_SECONDS`. There is no heartbeat yet, so a handler
that outlives its lease can be reclaimed and run twice.

## Known limitations (PR-2-03)

- **Recovery is worker-driven.** The sweep runs inside each worker. If no worker is
  running, nothing is reclaimed or terminated.
- **At-least-once, not exactly-once.** A reclaimed job runs its handler again. If the
  handler had already produced an external side effect, that effect can happen twice.
  Making side effects safe needs an outbox and reconciliation — PR-2-04 / ADR 0011.
- **No heartbeat.** A handler that outlives `LEASE_SECONDS` can be reclaimed mid-run
  and executed twice.
- **No `Idempotency-Key`.** PR-2-04.
- **No list endpoint, no auth, one `echo` handler.** Out of scope for this slice.

## Lint and test

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/async_job_api_lab
uv run ruff check . && uv run ruff format --check .
REQUIRE_DATABASE=1 uv run pytest -q
```

Without `TEST_DATABASE_URL`, migration integration tests skip (CI postgres job sets `REQUIRE_DATABASE=1`).

## Environment

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Required at app startup and for Alembic |
| `TEST_DATABASE_URL` | Disposable DB for integration tests |
| `REQUIRE_DATABASE` | `1` fails CI if database tests are skipped |
| `APP_ENV` | Reserved for later worker/API modes |
| `WORKER_POLL_SECONDS` | Optional, default `1.0`. Worker sleep between empty polls; must be finite, `> 0` and `<= 3600` |
| `LEASE_SECONDS` | Optional, default `60`. Claim lease length; must be `> 0` and `< 3600` |
| `RETRY_BACKOFF_SECONDS` | Optional, default `5`. Base retry delay; must be `> 0` and `<= 3600` |
| `MAX_RETRY_BACKOFF_SECONDS` | Optional, default `300`. Backoff ceiling; `> 0`, `<= 3600`, and `>= RETRY_BACKOFF_SECONDS` |

These four are read by the worker only. Invalid values raise `ConfigError` and the
worker exits non-zero rather than starting with a nonsensical lease or backoff.

The worker survives a bad cycle rather than exiting: a sweep that raises, a claim
that raises, a handler that raises or returns a non-object, and a result the
database refuses to store are each logged as `error_class` under
`worker.cycle_failed` or the relevant job event, and the loop polls again.

Surviving is not the same as recovering, but recovery now covers the gap: if the
worker cannot even write the failure (the requeue itself errors), the row is left
`RUNNING` on a live-or-expiring lease, and the next cycle either reclaims it —
attempts remain — or sweeps it to `FAILED` when they do not. That is deliberately
not retried in place: the write may have failed because the lease is already gone,
and re-writing under a lost lease would overwrite the new owner's row.

`jobs.result` has no `jsonb_typeof(result) = 'object'` constraint: the worker
refuses to store a non-object result, and `GET /v1/jobs/{id}` serves a non-object
result as `null`, but a database-level guarantee needs a migration that this branch
(already at `0004_job_retry`) does not carry.

## Known limitation (logging)

The worker logs no payload, no result, no handler error message and no traceback;
failures are recorded as `error_class` plus `last_error_chars` (the length of the
message that was stored on the row). The HTTP error handlers in
`app/errors.py` log an exception's stack frames and SQLSTATE but never its message,
because psycopg's `DETAIL: Failing row contains (...)` quotes the submitted row.
Frames name call sites, not values, so a database failure is diagnosable without
echoing the request body.

## ADRs

- [0010 Postgres job store](../docs/decisions/0010-postgres-job-store.md) (Accepted)
- [0011 Job idempotency](../docs/decisions/0011-job-idempotency.md) (Proposed)
