# 03 — API hardening: the asset registry

A small multi-tenant asset registry, written for Phase 3. It is a real service rather
than a scaffold: every endpoint below is backed by SQLite through the stdlib `sqlite3`
module, and the test suite runs against a real database file, not a stub.

The module exists to be investigated. A hardening review starts from a report about
behaviour, not from a list of known defects, so this service is a working product whose
behaviour can be probed, traced and changed like any other.

## Product surface

| Endpoint | Behaviour |
|---|---|
| `POST /v1/assets` | Create an asset for the calling tenant; `201` with the record |
| `GET /v1/assets/{id}` | Read one asset, including its tags |
| `GET /v1/assets` | List the caller's assets, newest first |
| `POST /v1/assets/{id}/tags` | Add tags to an asset; returns the asset with all its tags |
| `POST /v1/batch/reprocess` | Re-derive the stored fingerprint for a batch of the caller's assets |
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
failures name the offending field and never echo the submitted value.

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
