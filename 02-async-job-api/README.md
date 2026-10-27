# 02 — Async job API

## Progression

1. **PR-2-00 (Foundation):** Done ([#11](https://github.com/rlancaster243/api-engineering-lab/pull/11)) — install, `DATABASE_URL`, Alembic, `GET /health`, CI postgres job.
2. **PR-2-01 (Schema + submit):** Done (`dd365f2`) — `jobs` table (ADR 0010), `POST /v1/jobs` → 202 after commit, `GET /v1/jobs/{id}`.
3. **PR-2-02 (Worker):** Claim/lease protocol, handler registry, separate worker process.
4. **PR-2-03 (next):** Recovery — retries, lease expiry, crash falsification tests.
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
`DATABASE_URL` and claims `PENDING` jobs one at a time:

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

Events: `worker.started`, `worker.stopped`, `job.claimed`, `job.succeeded`,
`job.failed`, `job.lease_lost`. Payloads and results are never logged, and neither
is a handler's error message: `job.failed` carries `error_class` and
`last_error_chars`, while the message itself is stored on the job row and served
by `GET /v1/jobs/{id}`.

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

`WORKER_POLL_SECONDS` and `LEASE_SECONDS` are read by the worker only. Invalid
values raise `ConfigError` and the worker exits non-zero rather than starting with
a nonsensical lease.

## Known limitation (PR-2-02)

There is **no lease expiry recovery yet**. A worker that crashes mid-job leaves
the row `RUNNING` with a lease that expires and is then never reclaimed — the job
is stuck until PR-2-03 adds recovery. `attempt` is consumed at claim time (ADR
0010), so the crash is still recorded against the job's attempt budget.

The worker survives a bad cycle rather than exiting: a claim that raises, a handler
that raises or returns a non-object, and a result the database refuses to store are
each logged as `error_class` and the loop polls again. Surviving is not recovering.
A failure that repeats inside the worker's own cycle — after the row is already
claimed — leaves that job `RUNNING`, holding an expired lease, and nothing sweeps
it; the measured behaviour is one failure logged, the row left at its claimed
`attempt`, and the worker idling until the next poll finds new work. That is the
same gap as a crashed worker, and the same fix: the sweep in #17.

`jobs.result` has no `jsonb_typeof(result) = 'object'` constraint: the worker
refuses to store a non-object result, and `GET /v1/jobs/{id}` serves a non-object
result as `null`, but a database-level guarantee needs a migration that this PR
(already at `0003_job_leases`) does not carry.

## Known limitation (logging)

The worker logs no payload, no result, no handler error message and no traceback;
failures are recorded as `error_class` alone. The HTTP error handlers in
`app/errors.py` log an exception's stack frames and SQLSTATE but never its message,
because psycopg's `DETAIL: Failing row contains (...)` quotes the submitted row.
Frames name call sites, not values, so a database failure is diagnosable without
echoing the request body.

## ADRs

- [0010 Postgres job store](../docs/decisions/0010-postgres-job-store.md) (Accepted)
- [0011 Job idempotency](../docs/decisions/0011-job-idempotency.md) (Proposed)
