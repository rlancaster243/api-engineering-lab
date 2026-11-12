# 03 — API hardening: the asset registry

A small multi-tenant asset registry from Phase 3 of the lab. It is a real service rather
than a scaffold: every endpoint below is backed by SQLite through the stdlib `sqlite3`
module, and the test suite runs against a real database file, not a stub.

**Project 3 is complete on `main`** through the investigation PRs and [#28](https://github.com/rlancaster243/api-engineering-lab/pull/28) (53 tests, zero skips). The service was written to be investigated: defects were seeded for a blind hardening exercise and are now closed (see **Fixed** below). Completion record:
[`docs/evidence/project-3-completion.md`](../docs/evidence/project-3-completion.md).

## Product surface

| Endpoint | Behaviour |
|---|---|
| `POST /v1/assets` | Create an asset for the calling tenant; `201` with the record |
| `GET /v1/assets/{id}` | Read one asset, including its tags |
| `GET /v1/assets` | List the caller's assets, newest first |
| `POST /v1/assets/{id}/tags` | Add tags to an asset; returns the asset with all its tags |
| `POST /v1/batch/reprocess` | Re-derive the stored fingerprint for a batch of the caller's assets; `200` when at least one item was processed, `207` when none was |
| `GET /health` | Liveness. Does not touch storage |
| `GET /ready` | `SELECT 1` through the app's own connection |

Every `/v1` route requires an `X-API-Key` header. A key resolves to a tenant; there is
no unauthenticated fallback and no default tenant. The probes are deliberately open,
since an orchestrator has no credential to present.

## Layering

```
app/main.py        application object, lifespan, probes
app/config.py      environment -> Settings, failing closed without APP_ENV or DATABASE_PATH
app/db.py          connection per request, schema
app/auth.py        X-API-Key -> Tenant
app/errors.py      the error envelope and its handlers
app/schemas.py     request/response models and the enforced limits
app/repository.py  tenant-scoped SQL
app/service.py     use cases and transaction boundaries
app/routers/       HTTP surface
```

The same shape as `01-rest-api/`: a dependency resolves the caller, a service takes the
caller's identity plus a body and returns a response model, a repository is the only
place that writes SQL, and `app/errors.py` renders every failure — including framework
ones — as one envelope with a server-generated `request_id`.

## Error envelope

```json
{"error": {"code": "ASSET_NOT_FOUND", "message": "Asset not found.", "request_id": "..."}}
```

`request_id` is always server-generated and also returned in `X-Request-ID`. Validation
failures name the offending field and never echo the submitted value. A `5xx` body is the
fixed message for its code — `{"code": "INTERNAL_ERROR", "message": "An unexpected error
occurred."}` — and never the exception's own text: the exception and traceback are logged
on a line carrying the same `request_id`, which is how a report is matched to the fault.

## Batch outcomes

`POST /v1/batch/reprocess` answers per item and keeps the detail in every case:

| Outcome | Status |
|---|---|
| Every item reprocessed | `200` |
| Some reprocessed, some failed | `200` — `results` marks each item |
| No item reprocessed, at least one failed | `207 Multi-Status` |

`207` and not `503`: the request was well formed and the server was healthy, so `503`
would name an outage that is not happening and invite a retry that fails identically.
`ApiError` has no details channel, so raising a contract error would also discard the
per-item `results` that say *why* each id failed.

## Run locally

```bash
cp .env.example .env
uv sync --locked --extra dev
set -a && . ./.env && set +a    # APP_ENV and DATABASE_PATH must reach the process
uv run uvicorn app.main:app --reload --port 8003
```

`APP_ENV` is required and has no default: without it the service refuses to start.
Copying `.env` is not enough on its own — uvicorn only reads that file when passed
`--env-file .env`, so either use the `set -a && . ./.env && set +a` line above or run
`uv run uvicorn app.main:app --reload --port 8003 --env-file .env`.

`APP_ENV` must be one of `local`, `test` or `production`; any other value also stops
startup. `APP_ENV=local` seeds two documentation tenants, so the service is usable with
`curl` without a setup step:

```bash
curl -s http://127.0.0.1:8003/health

ASSET=$(curl -s -X POST http://127.0.0.1:8003/v1/assets \
  -H 'X-API-Key: local-dev-key-alpha' \
  -H 'Content-Type: application/json' \
  -d '{"name": "Q3 report", "source_uri": "https://files.example.test/q3.pdf",
       "content_type": "application/pdf", "size_bytes": 4096}')

echo "$ASSET" | jq .
ID=$(echo "$ASSET" | jq -r .id)

curl -s "http://127.0.0.1:8003/v1/assets/$ID" -H 'X-API-Key: local-dev-key-alpha' | jq .
curl -s "http://127.0.0.1:8003/v1/assets" -H 'X-API-Key: local-dev-key-alpha' | jq .
curl -s -X POST "http://127.0.0.1:8003/v1/assets/$ID/tags" \
  -H 'X-API-Key: local-dev-key-alpha' -H 'Content-Type: application/json' \
  -d '{"tags": ["finance", "quarterly"]}' | jq .
curl -s -X POST http://127.0.0.1:8003/v1/batch/reprocess \
  -H 'X-API-Key: local-dev-key-alpha' -H 'Content-Type: application/json' \
  -d "{\"ids\": [\"$ID\"]}" | jq .

# The other tenant cannot see it in a list
curl -s "http://127.0.0.1:8003/v1/assets" -H 'X-API-Key: local-dev-key-beta' | jq .
```

The seeded keys exist only for `APP_ENV=local` and `APP_ENV=test`, and they are published
here, so they are not credentials: anything that reaches `local-dev-key-alpha` has no
authentication in front of it. In any other environment `DATABASE_PATH` is required, no
tenant is seeded, and both keys are a `401`. Because `APP_ENV` has no default, a
deployment that sets `DATABASE_PATH` for real but forgets `APP_ENV` stops at startup
instead of silently seeding them.

## Test

```bash
uv sync --locked --extra dev
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

Each test gets its own SQLite file in a temporary directory and two tenants, so the
tests exercise the real schema, the real constraints and the real commit boundaries.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_PATH` | `var/assets.sqlite3` in `local`/`test`, required otherwise | SQLite file |
| `APP_ENV` | none — startup fails if unset | `local`/`test` enable the sample tenants; `production` disables them. Any other value fails startup |

## Persistence choice

SQLite through stdlib `sqlite3`, not PostgreSQL. See
[ADR 0012](../docs/decisions/0012-sqlite-for-project-3.md) for the reasoning and for
what the choice does not cover.

## Known limitations

- One file, one writer at a time. SQLite serialises writers, so this is not a
  deployment story for concurrent write load; `timeout=5.0` turns sustained contention
  into a `503` rather than an indefinite hang.
- No pagination cursor. `GET /v1/assets` takes `limit` only, so a tenant with more
  assets than the limit cannot walk past the first page.
- `checksum` is derived from the *declared* metadata (URI, content type, size), not
  from the bytes. Nothing in this service fetches the object.
- No rate limiting. Keys are per tenant and there is no quota.

## Fixed

Three defects found during the hardening review and closed in follow-ups; all are recorded
here so a reader does not treat the description above as the whole story.

- **Exception text reached the client.** An unexpected exception rendered `str(exc)` in the
  `500` envelope, so a storage fault answered with driver text such as
  `no such table: asset_tags` (relation names, file paths). Every `5xx` now returns the
  fixed message for its code; the exception stays on the log line with the `request_id`
  the response returns. Covered by
  `tests/test_errors.py::test_a_storage_failure_does_not_quote_the_driver` and
  `test_contention_is_unavailable_without_the_driver_text`, and by
  `tests/test_ready.py::test_ready_is_unavailable_when_the_file_cannot_be_opened`.
- **A batch reported success when it did nothing.** `reprocess` returned `200` with
  `{"processed": 0, "failed": 3}` when every id failed. That case is now `207`, keeping the
  per-item `results`; partial and full success stay `200`. Covered by
  `tests/test_batch.py::test_reprocess_answers_207_when_every_item_failed`.
- **A tag write was not atomic with its parent's `updated_at`.** `POST
  /v1/assets/{id}/tags` committed the tag rows, then advanced the asset's `updated_at` in a
  second transaction, so a failure between the two commits left a tag the caller had been
  told was not created, against an `updated_at` that never moved — a consumer ordering by
  `updated_at` missed the change. Both statements now sit in `AssetRepository.add_tags`
  ahead of its single `commit()`, and the service no longer calls a second repository method
  for the timestamp (`touch` was removed with its only caller). A failed request now leaves
  no tag row and does not move the timestamp, so retrying is the whole recovery.
  `tests/test_tags.py::test_a_failed_tag_write_leaves_no_tag_row_and_does_not_advance_the_timestamp`
  covers it; the previous test that pinned the split write was inverted, not added to.
