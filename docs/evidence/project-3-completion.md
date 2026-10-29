# Project 3 completion record

**Repository:** `03-api-hardening/` — multi-tenant asset registry
**Status:** Complete on `main` at `67aae39`. The investigation fixed one planted defect
([#24](https://github.com/rlancaster243/api-engineering-lab/pull/24), `20eac5c`), one defect
nobody planted ([#25](https://github.com/rlancaster243/api-engineering-lab/pull/25),
`cb3b2b1`), and two adjacent defects in
[#26](https://github.com/rlancaster243/api-engineering-lab/pull/26), `67aae39`. The suite is
53 passed, zero skips. One planted defect — the split tag/timestamp transaction — is still
present and was deferred deliberately; see [Known limitations](#known-limitations).

## What the module is

`03-api-hardening/` is a working multi-tenant asset registry written for this phase: create,
read, list, tag, batch-reprocess, liveness and readiness, every `/v1` route behind an
`X-API-Key` resolved to a tenant, persistence through stdlib `sqlite3` (ADR 0012). It landed
in [#23](https://github.com/rlancaster243/api-engineering-lab/pull/23) (`e873081`) with 38
tests passing.

It is an **explicitly authorized reproduction**. Syllabus §9 permits "a sanitized,
explicitly authorized reproduction of an existing API", and that is what this is: a service
written from scratch inside this repository for the purpose of being investigated. **The
defects were seeded, not discovered in production.** No proprietary third-party
implementation, data, credential or internal document was copied, read into this module, or
published in it. The §13 decision that selected this route is recorded in the syllabus.

## Merged pull requests

| PR | Commit | Scope |
|----|--------|-------|
| [#23](https://github.com/rlancaster243/api-engineering-lab/pull/23) | `e873081` | The asset registry itself: schema, tenant-scoped repository, service layer, error envelope, 38-test suite passing while four defects are present, ADR 0012 filed as Proposed |
| [#24](https://github.com/rlancaster243/api-engineering-lab/pull/24) | `20eac5c` | Blind investigation of the escalated report: tenant predicate added to `get_by_id` and `tags_for`, regression test added |
| [#25](https://github.com/rlancaster243/api-engineering-lab/pull/25) | `cb3b2b1` | The unplanted finding: `APP_ENV` required, no default anywhere, local mode decided once from the loaded `Settings` |
| [#26](https://github.com/rlancaster243/api-engineering-lab/pull/26) | `67aae39` | D2 exception leak closed, D4 no-op batch answers `207`, README corrected, D3 kept as a known limitation |

Test counts move with each commit, measured on this tree: **38** at `e873081`, **39** at
`20eac5c`, **51** at `cb3b2b1`, **53** at `67aae39`.

## What the investigation demonstrates — and what it does not

This is the part worth being exact about, because the setup is easy to describe as something
stronger than it is.

**It was blind.** The investigation in [#24](https://github.com/rlancaster243/api-engineering-lab/pull/24)
was run by a separate agent that received one user-visible symptom and nothing else:

> A customer forwarded an asset link to someone at a different company, and that person
> could open it. We expected a 404. Nobody has touched this code in weeks and the test suite
> is green.

No file, no line, no function name, no hint at which of the four seeded defects was the
reported one, and — the control that makes the word meaningful — no access to the written
answer key. Blindness here is enforced by withholding the file, not by an honour system; the
key names D1 as the primary defect and D2 as the intended distractor, and none of that
reached the investigator.

**So the reasoning is real.** The reproduction, the trace from route to SQL, the isolation of
the earliest divergence, the failing-before test and the rejection of the plausible
Python-side fix were all produced under uncertainty. The investigator did not know where the
fault was while looking for it, and that is the property that makes the trace evidence of
skill rather than narration of a disclosed answer.

One claim in this section needed correcting, and the correction is more useful than the
original would have been. An earlier draft repeated, from its own source material, the answer
key's prediction that adding `row["tenant_id"] != self.tenant_id` to `AssetRegistry.get` after
`get_by_id` returns would pass a naive `404` test — a near miss only the mechanism argument
would catch. **That prediction is wrong.** Applied alone to the unfixed tree, the filter raises
`IndexError: No item with that key`, because `ASSET_COLUMNS` does not select `tenant_id`, so
the row has no such key to compare. It passes only once `tenant_id` is *also* added to the
select list — and at that point `repository.get_by_id` still returns the foreign row, which is
the actual reason the approach is wrong: the unsafe query stays reachable and the boundary is
still decided in Python, after the row has been lifted into the process. The near miss is
therefore narrower than the key documented, and it was never a trap a test would have sprung.
The investigator rejected the shape on mechanism grounds without relying on a test outcome —
correct reasoning either way, and it is the reasoning that is credited here, not a trap that
was not there.

That correction belongs in the record. The repository owner's independent verification, not
this document, found and corrected the error in the answer key: the claim was tested against
the code and disproved before it was written down as fact. A completion record that quietly
repeats its own input is worth less than one that shows a claim was tested and where it turned
out to be wrong.

**What it does not demonstrate.** It is not evidence of finding defects in production, under
time pressure, with incomplete information, on a system nobody wrote an answer key for. There
was no on-call pressure, no production data, no ambiguity about whether the behaviour was
even wrong, and — decisively — a defect was *known to exist* before the search started. The
syllabus §14 rule applies directly: **do not collapse these levels.** Documented, implemented,
tested, demonstrated and operationally validated are separate rungs. This phase reaches the
"tested and demonstrated on a purpose-built service" rung. It does not reach
"operationally validated", and it does not reach "independent debugging of an unfamiliar
production system".

**The strongest evidence in this phase is arguably the unplanted finding, not the planted
one.** The cross-tenant read was found by an investigator who had been told a cross-tenant
read was happening — a real exercise in *locating* a defect, but not in *discovering* that
one exists. The `APP_ENV` fail-open default was seeded by nobody. It is not in the answer key,
it is not one of the four planned defects, and it was reported in [#24](https://github.com/rlancaster243/api-engineering-lab/pull/24)'s
adjacent-findings section as something noticed in passing while tracing the read path. A
blind agent that reports a genuine defect its brief did not contain is demonstrating
something the planted defect cannot: that it was reading the system rather than following a
report. Both matter; the second is harder to arrange and harder to fake.

## The planted defect — cross-tenant read on `GET /v1/assets/{id}`

### Symptom

An asset link forwarded outside its tenant opened for the recipient. Expected `404`; actual
`200` with the full record. The suite was green both before and after the report.

### Reproduction

Tenant Alpha creates an asset and tags it; Tenant Beta opens the forwarded id.

```http
POST /v1/assets
X-API-Key: local-dev-key-alpha
{"name":"Q3 report","source_uri":"https://files.example.test/q3.pdf",
 "content_type":"application/pdf","size_bytes":4096}

201
{"id":"cad351b6-9a4c-49ac-b8b4-d0a40bab9c5c","name":"Q3 report", ... }
```

```http
GET /v1/assets/cad351b6-9a4c-49ac-b8b4-d0a40bab9c5c
X-API-Key: local-dev-key-beta
```

Actual, before the fix:

```http
HTTP/1.1 200 OK
x-request-id: 952ad538-17c8-4545-b868-f2ad1149d590
content-type: application/json

{"id":"cad351b6-9a4c-49ac-b8b4-d0a40bab9c5c","name":"Q3 report","source_uri":"https://files.example.test/q3.pdf","content_type":"application/pdf","size_bytes":4096,"checksum":"c440a6446b2426ad4189e2a51c4f209a6052b3aa7dff88e8a6e37b570dc77926","metadata_version":1,"created_at":"2026-10-28T15:59:53.015878+00:00","updated_at":"2026-10-28T15:59:56.739947+00:00","tags":["board-only","finance"]}
```

Full record disclosure, not only existence: name, `source_uri`, size, checksum, metadata
version and the asset's tags all crossed the tenant boundary. The neighbouring endpoints were
probed in the same session and were correctly scoped — Beta's `GET /v1/assets` returned
`{"assets":[]}`, `POST /v1/assets/{id}/tags` returned `404`, and batch reprocess reported
`not_found`. The asymmetry between the collection read and the single-row read is the whole
finding.

### Trace

```
GET /v1/assets/{asset_id}
  app/routers/assets.py        get_asset -> registry.get(asset_id)
  app/routers/assets.py        get_registry(request, tenant: CurrentTenant, connection)
                                 -> AssetRegistry(connection, tenant.id)
  app/auth.py                  current_tenant(): X-API-Key -> sha256 -> api_keys JOIN tenants
                                 -> Tenant(id=...)                    # identity resolved correctly
  app/service.py               AssetRegistry.get(asset_id):
                                 row = self.repository.get_by_id(asset_id)   # <-- tenant dropped
                                 if row is None: raise ApiError(ASSET_NOT_FOUND)
  app/repository.py            get_by_id(asset_id):
                                 SELECT {ASSET_COLUMNS} FROM assets WHERE id = ?   # <-- divergence
  app/errors.py                ASSET_NOT_FOUND -> 404 "Asset not found." envelope
```

Identity resolution is not the fault: the right tenant flowed all the way into
`AssetRegistry.__init__` and was simply never used by the one statement that decides
reachability.

### Earliest divergence

Not the service's `if row is None` branch — it never ran, because the row it was handed was
never `None`. Not `tags_for` — it correctly rendered the tags of the row it was given. The
first point where expected and actual differ is the `WHERE` clause of the single-row read in
`app/repository.py`, which returns a row for an id the caller does not own. Everything
downstream is faithful handling of the wrong row set. That is why the fix belongs in the
statement and not in the service: a Python-side `tenant_id` comparison after the load would
leave the unsafe query reachable from every future caller, which is the shape the repository
docstring explicitly claims not to use — "which rows a caller can reach is decided by the
database rather than by a comparison in Python after a row has already been loaded into the
process." (That filter would not have worked as first proposed: it raises `IndexError`, not
`404`. See the correction in
[What the investigation demonstrates](#what-the-investigation-demonstrates--and-what-it-does-not).
It needs `tenant_id` added to the select list before it can compare anything, which is a second
change — and it still leaves `get_by_id` returning the foreign row.)

### Failing before

`tests/test_assets.py::test_get_of_an_unowned_asset_is_not_found`, written first and run
against the parent commit `e873081`:

```text
E       assert 200 == 404
E        +  where 200 = <Response [200 OK]>.status_code

tests/test_assets.py:97: AssertionError
FAILED tests/test_assets.py::test_get_of_an_unowned_asset_is_not_found - asse...
1 failed, 1 warning in 0.09s
```

A rendered-response assertion on a real request through the lifespan and a real SQLite file.
The assertion that fails is the status code, which is the defect itself — not an import,
fixture or setup error. Re-checked for this record on a clean worktree of `e873081` with only
that test added: `assert 200 == 404`.

No pre-existing test asserted the defective behaviour. The suite was green because the case
was untested, not because it was blessed, which is why "the suite is green" was never
evidence here and the investigator said so.

### The fix

`WHERE id = ? AND tenant_id = ?` on the one statement that lacked the predicate, and the
tenant passed in at three call sites:

```diff
 app/repository.py   def get_by_id(self, tenant_id: str, asset_id: str):
-        f"SELECT {ASSET_COLUMNS} FROM assets WHERE id = ?", (asset_id,)
+        f"SELECT {ASSET_COLUMNS} FROM assets WHERE id = ? AND tenant_id = ?",
+        (asset_id, tenant_id),

 app/repository.py   (in create)      row = self.get_by_id(tenant_id, asset_id)
 app/service.py      (in get)         row = self.repository.get_by_id(self.tenant_id, asset_id)
 app/service.py      (in add_tags)    row = self.repository.get_by_id(self.tenant_id, asset_id)
```

The same PR also scoped `tags_for` by joining `asset_tags` to `assets` on `tenant_id`, so tag
reads follow the repository contract rather than depending on a pre-scoped caller.
`ASSET_NOT_FOUND` was reused rather than introducing a new code, so a foreign id and an
absent id are indistinguishable to the caller and the response adds no existence oracle. The
method now requires the tenant as a positional argument, so omitting it is a `TypeError`
rather than a silent cross-tenant read.

### Passing after

```text
$ uv run pytest -q tests/test_assets.py::test_get_of_an_unowned_asset_is_not_found
1 passed, 1 warning        # 39 passed for the whole suite at 20eac5c
```

Live server, Beta reading Alpha's asset:

```http
HTTP/1.1 404 Not Found
x-request-id: d2ca6915-2b8d-4c88-89eb-a2b3b4763a42

{"error":{"code":"ASSET_NOT_FOUND","message":"Asset not found.","request_id":"d2ca6915-2b8d-4c88-89eb-a2b3b4763a42"}}
```

Alpha reading its own asset still returned `200`.

### Blast radius

- **Exposure window:** the defect shipped in `e873081` (#23). Every `GET /v1/assets/{id}`
  served in that window could return another tenant's asset to any caller holding a valid
  key. No code change was needed to trigger it — only a valid key and a known asset id.
- **Data disclosed:** the full detail record, including tags. In the reproduction those were
  `board-only` and `finance`, which in practice is often the most sensitive part.
- **Discovery is bounded, the incident is not:** asset ids are UUIDv4 and not enumerable, so
  exploitation requires an id to leak — exactly the forwarded-link case in the report.
  Opportunistic scanning is not practical; the reported incident is not covered by that
  bound.
- **Not affected:** list, tag-write and batch-reprocess paths, verified in the same session.
- **No read-audit log exists**, so the set of disclosed assets cannot be reconstructed from
  application data. Forwarded ids should be treated as disclosed.

### Rollback

Revert one commit and redeploy. The change is a `WHERE` clause plus call-site arguments: no
schema migration, no data backfill, no configuration change, no new dependency, and no stored
state touched in either direction. Existing clients reading their own assets see identical
responses. Rolling back **re-opens the disclosure**, so it is only for restoring service after
an unforeseen failure in the fix, and the exposure should be re-triaged if it happens.

## The unplanted finding — `APP_ENV` fell open to `local`

Nobody seeded this one. It is not in the answer key, it is not one of the four planned
defects, and it was reported as an adjacent finding in
[#24](https://github.com/rlancaster243/api-engineering-lab/pull/24) rather than fixed there,
so the primary fix stayed isolated.

### The defect

`APP_ENV` defaulted to `local` in three places, and each one failed open:

1. `load_settings` — `os.environ.get("APP_ENV", "local").strip() or "local"`.
2. The `Settings` dataclass — `app_env: str = "local"`, a second copy of the same decision.
3. `auth.local_env()` — re-read `os.environ` with the same fallback, and never saw the
   `Settings` the lifespan had already loaded. Two independent reads of one variable that
   could disagree.

`prepare_database` consulted `local_env()`, so a deployment that set `DATABASE_PATH` for real
— the signal that somebody was configuring the service on purpose — and omitted `APP_ENV`
started in local mode, seeded the two documentation tenants, and accepted the API keys
published in the module README. The service also claimed, in the `config.py` docstring, to
fail closed in production.

### Verified on the pre-fix commit

On a clean worktree of `e873081`, with `DATABASE_PATH` set and `APP_ENV` absent:

```text
AssertionError: startup did not fail closed: the README key authenticated with 200
```

The published key `local-dev-key-alpha` answered `200` on `GET /v1/assets` against a
deployment that had only forgotten one variable. With `APP_ENV=production` on the same code
the same request was `401`, which is what made the unset case a silent mode change rather
than a known limitation.

### The fix

`cb3b2b1` ([#25](https://github.com/rlancaster243/api-engineering-lab/pull/25)):

- `APP_ENV` is required. `load_settings` raises `RuntimeError` when it is unset or not one of
  `local`, `test`, `production`, in the same style as the adjacent `DATABASE_PATH` check.
- No default anywhere. `Settings.app_env` loses its default so the dataclass cannot decide
  local mode either, and `local_env(settings: Settings)` takes the loaded settings instead of
  re-reading `os.environ`. `prepare_database` takes `Settings`, so the seeding call uses the
  value the lifespan read once.
- Unknown values refuse to start rather than resolving to "not local". A typo such as
  `APP_ENV=prod` is an operator mistake, and silently enabling *or* disabling the documented
  tenants is worse than refusing to boot.

The breaking change is deliberate: a deployment that relied on the default now stops at
startup.

### Verified after

- `APP_ENV` unset, `DATABASE_PATH` set:
  `RuntimeError: APP_ENV is required; set it to one of local, test, production`, then
  `ERROR: Application startup failed. Exiting.`
- `APP_ENV=production`, published key: `401`.
- `APP_ENV=local`, published key: `200`, and the documented quick start still works end to
  end (`POST /v1/assets` `201`, detail and list `200`, tags `200`, batch `200`, the other
  tenant's list empty).

All three re-checked against a live uvicorn for this record, not only through the test client.

### Why this is the stronger evidence

The planted defect was *known to exist* before the search began; the search was for its
location. This one was not. A blind investigator that reports a real defect absent from its
brief — and, correctly, reports it separately instead of bundling it into the fix — has
demonstrated that it was reading the system rather than following the report. It is still not
production debugging: the module is small, the investigator wrote none of the surrounding
pressure into the story because there was none, and the finding took a trace that was already
underway to reach. It is the strongest evidence in the phase, not unlimited evidence.

## The two follow-up fixes

Both in `67aae39` ([#26](https://github.com/rlancaster243/api-engineering-lab/pull/26)).

### D2 — exception text reached the client

An unexpected exception rendered `str(exc)` in the `500` envelope, so a storage fault
answered with driver text such as `no such table: asset_tags` — relation names and, for other
drivers, file paths and connection strings. Every handler funnelled through one function, so
removing one argument closed it on all of them:

```diff
-    return error_response(request, ErrorCode.INTERNAL_ERROR, message=str(exc) or None)
+    return error_response(request, ErrorCode.INTERNAL_ERROR)
```

A `5xx` body is now the fixed message for its code — `An unexpected error occurred.` for
`INTERNAL_ERROR`, `The service is temporarily unavailable.` for `SERVICE_UNAVAILABLE` — and
the exception and traceback stay on a log line carrying the same `request_id` the response
returns, which is how a support report still resolves to the fault. `/ready` was already
correct and gained assertions pinning it, because a probe body lands in orchestration logs.

Three existing tests had asserted the leak, which is what the seeded suite was designed to do.
They were inverted rather than deleted: the new assertion is stricter — the fixed message
*and* the relation absent from the whole serialised response.

### D4 — a batch that processed nothing answered `200`

`reprocess` returned `200` with `{"processed": 0, "failed": 3}` when every id failed, so a
client that branched on the status recorded a successful reprocess of assets it never
touched. Partial failure staying `200` with per-item detail is defensible and unchanged; the
zero-success case is the defect. The rule is now one line in the router:

```python
if summary.processed == 0 and summary.failed:
    response.status_code = 207
```

`207` and not `503`: the request was well formed and the server was healthy, so `503` would
name an outage that is not happening and invite a retry that fails identically — every id in
this case is a client-side outcome (`not_found`, `not_writable`). `207` also keeps the
per-item `results` body, which raising a contract error would have discarded, because
`ApiError` has no details channel.

| Outcome | Status |
|---|---|
| Every item reprocessed | `200` |
| Some reprocessed, some failed | `200` — `results` marks each item |
| No item reprocessed, at least one failed | `207 Multi-Status` |

## Test-driven mutation results

Each fix was reverted alone on the fixed tree, the suite re-run, then restored. Quotes below
are from the PR bodies; the same mutations were re-run on this branch for the record and
produced the same named failures and counts.

**#24 — tenant predicate** (`1 failed` when reverted, `1 passed` restored):

```text
=== deliberate break: revert WHERE tenant_id ===
FAILED tests/test_assets.py::test_get_of_an_unowned_asset_is_not_found - asse...
1 failed, 1 warning in 0.07s
=== restored ===
1 passed, 1 warning in 0.07s
```

**#25 — three mutations, one per default:**

| Reverted | Result | Named test that went red |
|---|---|---|
| `load_settings` default | `2 failed, 9 passed, 1 warning in 0.11s` | `test_omitting_app_env_raises_instead_of_defaulting_to_local`, `test_forgotten_app_env_never_exposes_the_published_keys` |
| `local_env` re-reads `os.environ` | `2 failed, 10 passed, 1 warning in 0.12s` | `test_local_env_reads_settings_not_the_environment`, `test_startup_seeding_follows_settings_not_a_second_environment_read` |
| `Settings.app_env` default | `1 failed, 11 passed, 1 warning in 0.11s` | `test_settings_requires_an_environment` |

After each revert: `12 passed`. The second failure under the first mutation is the original
defect reproduced verbatim — the published key authenticated again.

**#26 — three mutations:**

| Reverted | Result | Named test that went red |
|---|---|---|
| D2: restore `message=str(exc) or None` | `1 failed, 52 passed` | `tests/test_errors.py::test_a_storage_failure_does_not_quote_the_driver` |
| D4: drop the `207` branch | `2 failed, 51 passed` | `tests/test_batch.py::test_reprocess_answers_207_when_every_item_failed`, `tests/test_errors.py::test_a_malformed_identifier_does_not_reach_the_database` |
| `/ready` guard: interpolate the driver text | `1 failed, 4 passed` (test_ready.py only) | `tests/test_ready.py::test_ready_is_unavailable_when_the_file_cannot_be_opened` |

Restored tree after each: `53 passed`.

The tests that support these fixes and are not themselves mutation targets:
`test_contention_is_unavailable_without_the_driver_text` (the `503` contention path also says
nothing specific), `test_reprocess_answers_200_when_something_succeeded` (partial failure
stays `200`), `test_unknown_app_env_is_rejected`, `test_production_startup_seeds_nothing`,
`test_local_env_agrees_with_the_seeding_decision` (parametrised over all three environments),
`test_local_app_env_seeds_the_documented_quick_start`, and
`test_seeding_local_tenants_is_idempotent`.

## Verification record

On `main` at `67aae39`, in `03-api-hardening/`:

```bash
uv sync --locked --extra dev
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
uv run pytest -q -rs
```

Actual results:

```text
All checks passed!
20 files already formatted
53 passed, 1 warning in 0.91s
```

Confirmed independently in CI on the same commit (`lint & pytest — 03-api-hardening`):
`53 passed, 1 warning in 1.19s`. **Zero skips** — `-rs` reports no skip lines. The single
warning is a `StarletteDeprecationWarning` from the installed `fastapi.testclient`, not from
this module's code.

Failing-before was re-established for this record rather than taken from the PR bodies:

- `test_get_of_an_unowned_asset_is_not_found` on a clean worktree of `e873081`:
  `assert 200 == 404`.
- `test_forgotten_app_env_never_exposes_the_published_keys` with `APP_ENV` absent on the same
  commit: `published key authenticated with 200`.
- `test_reprocess_answers_207_when_every_item_failed` on a worktree of `cb3b2b1` (the parent
  of the `207` change): `assert 200 == 207`.

No PostgreSQL involvement: this module is SQLite by ADR 0012 and nothing here exercises MVCC,
row locking, serialization failure or `READ COMMITTED` visibility. **§14 evidence for this
phase stops at "tested on SQLite".** It is not PostgreSQL transaction evidence, and nothing
in the D3 entry below should be read as a PostgreSQL locking finding.

## Known limitations

Mirrored from the current `03-api-hardening/README.md`, which is authoritative if the two
disagree.

- **D3 — a tag write is not atomic with its parent's `updated_at`.** `POST
  /v1/assets/{id}/tags` commits the tag rows, then advances the asset's `updated_at` in a
  second transaction. A failure between the two commits leaves a tag the caller was told was
  not created, and an `updated_at` that never moved, so a consumer ordering by `updated_at`
  misses the change. Present on `main` and **deferred rather than fixed**: closing it needs
  the transaction boundary moved into the repository so both statements share one commit, not
  a local patch at either call site. It is recoverable by retry — the inserts are idempotent,
  so a second request lands the same tags and repairs the timestamp. The current behaviour is
  pinned by `tests/test_tags.py::test_tag_write_survives_a_failure_after_the_tag_rows_land`,
  so closing it means inverting that test, not adding one.
- **One file, one writer at a time.** SQLite serialises writers, so this is not a deployment
  story for concurrent write load; `timeout=5.0` turns sustained contention into a `503`
  rather than an indefinite hang.
- **No pagination cursor.** `GET /v1/assets` takes `limit` only, so a tenant with more assets
  than the limit cannot walk past the first page.
- **`checksum` is derived from the declared metadata**, not from the bytes: URI, content type
  and size. Nothing in this service fetches the object, so two different objects with the same
  declared metadata fingerprint identically, and re-publishing different bytes without
  changing the declaration is invisible.
- **No rate limiting.** Keys are per tenant and there is no quota, so nothing bounds a caller's
  request rate.
- **The API keys are unauthenticated beyond possession.** There is no signature, no expiry, no
  rotation and no scope: a key is a bearer string, `local-dev-key-alpha` is published in the
  README, and anything holding any valid key has full read and write access to its tenant. The
  seeded keys exist only under `APP_ENV=local` or `test` and are `401` elsewhere, which is a
  seeding rule, not an authentication mechanism.

This is a lab system. It is not production-ready.

