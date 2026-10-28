"""Tests for ``Idempotency-Key`` on ``POST /v1/jobs`` (PR-2-04b, ADR 0011).

The gate of this slice is ``test_concurrent_submissions_of_one_key_create_one_job``:
eight threads submit the same key against the real HTTP surface and exactly one job
must exist afterwards. Everything else here is a rule from the ADR that would still
hold if the concurrency story were wrong, which is why the race is asserted with real
connections rather than a sequential replay.

Row counts are read through ``migrated_engine`` — a separate connection — so a
"replayed" answer backed by an uncommitted second row cannot pass.
"""

import json
import threading
import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

MAX_KEY_LENGTH = 255


@pytest.fixture(autouse=True)
def clean_jobs(migrated_engine: Engine) -> Generator[None, None, None]:
    """Empty ``jobs`` around every test in this module.

    The direct-SQL schema tests below bypass ``db_client``, whose own teardown only
    covers tests that used it, so without this a seeded row would follow the test
    into the next one.
    """
    with migrated_engine.connect() as conn:
        conn.execute(text("DELETE FROM jobs"))
        conn.commit()
    yield
    with migrated_engine.connect() as conn:
        conn.execute(text("DELETE FROM jobs"))
        conn.commit()


@pytest.fixture
def db_client(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    """TestClient wired to the migrated test database.

    Row cleanup is the autouse ``clean_jobs`` fixture's job, so this one only has to
    point the app at the test database.
    """
    url = migrated_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    from app.main import app

    with TestClient(app) as client:
        yield client


def job_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()


def stored_key(engine: Engine, job_id: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT idempotency_key FROM jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()


def keys_stored(engine: Engine) -> int:
    """How many rows carry a key at all — used where several rows are legal."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM jobs WHERE idempotency_key IS NOT NULL")
        ).scalar_one()


def body(payload: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "type": "echo",
        "payload": payload if payload is not None else {"msg": "hello"},
    }
    request.update(overrides)
    return request


# ---------------------------------------------------------------------------
# The key is optional
# ---------------------------------------------------------------------------


def test_without_a_key_identical_submissions_create_two_jobs(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """No header means the pre-existing contract: every POST is a new job."""
    first = db_client.post("/v1/jobs", json=body())
    second = db_client.post("/v1/jobs", json=body())

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
    assert "Idempotency-Replayed" not in first.headers
    assert "Idempotency-Replayed" not in second.headers
    assert job_count(migrated_engine) == 2


# ---------------------------------------------------------------------------
# Same key, same body → replay
# ---------------------------------------------------------------------------


def test_same_key_and_body_replays_the_same_job(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    key = "order-create-77c1"

    first = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})
    assert first.status_code == 202
    assert "Idempotency-Replayed" not in first.headers, "a create must not claim to be a replay"

    second = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})
    assert second.status_code == 202
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json()["id"] == first.json()["id"], "the replay must return the original job id"
    assert second.headers["Location"] == first.headers["Location"]
    assert second.json()["status"] == "PENDING"
    assert job_count(migrated_engine) == 1, "the replay created a second job"


def test_a_replay_does_not_rewrite_the_stored_row(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """A replay is a read; it must not bump ``updated_at`` on the row it replays."""
    key = "no-write-on-replay"
    first = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})
    job_id = first.json()["id"]

    with migrated_engine.connect() as conn:
        before = conn.execute(
            text("SELECT updated_at FROM jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()

    db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})

    with migrated_engine.connect() as conn:
        after = conn.execute(
            text("SELECT updated_at FROM jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()
    assert after == before, "the replay wrote to the job it replayed"


# ---------------------------------------------------------------------------
# Same key, different request → 409
# ---------------------------------------------------------------------------


def test_same_key_with_a_different_payload_conflicts(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    key = "payload-mismatch"
    first = db_client.post("/v1/jobs", json=body({"amount": 1}), headers={"Idempotency-Key": key})
    assert first.status_code == 202

    second = db_client.post("/v1/jobs", json=body({"amount": 2}), headers={"Idempotency-Key": key})
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    assert "request_id" in error
    assert "amount" not in second.text, "the conflicting body reached the response"
    assert job_count(migrated_engine) == 1


def test_same_key_with_a_different_max_attempts_conflicts(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """``max_attempts`` changes what the job does, so it is part of the identity."""
    key = "attempts-mismatch"
    first = db_client.post("/v1/jobs", json=body(max_attempts=3), headers={"Idempotency-Key": key})
    assert first.status_code == 202

    second = db_client.post("/v1/jobs", json=body(max_attempts=5), headers={"Idempotency-Key": key})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert job_count(migrated_engine) == 1


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def test_key_order_in_the_body_does_not_defeat_the_fingerprint(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """``{"b":1,"a":2}`` and ``{"a":2,"b":1}`` are the same request.

    Raw bytes are used because ``json=`` on a ``dict`` cannot express insertion
    order twice in one process without relying on literal ordering.
    """
    key = "canonical-order"
    first = db_client.post(
        "/v1/jobs",
        content=b'{"type": "echo", "payload": {"b": 1, "a": 2}}',
        headers={"Content-Type": "application/json", "Idempotency-Key": key},
    )
    assert first.status_code == 202, first.text

    second = db_client.post(
        "/v1/jobs",
        content=b'{"type": "echo", "payload": {"a": 2, "b": 1}}',
        headers={"Content-Type": "application/json", "Idempotency-Key": key},
    )
    assert second.status_code == 202, "key order produced a spurious 409"
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json()["id"] == first.json()["id"]
    assert job_count(migrated_engine) == 1


def test_fingerprint_ignores_default_versus_explicit_max_attempts(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """An omitted ``max_attempts`` defaults to 3, which is the same request as ``3``."""
    key = "default-attempts"
    first = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})
    assert first.status_code == 202

    explicit = body(max_attempts=3)
    second = db_client.post("/v1/jobs", json=explicit, headers={"Idempotency-Key": key})
    assert second.status_code == 202
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert job_count(migrated_engine) == 1


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------


def test_key_longer_than_255_chars_is_422_and_is_not_echoed(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    key = "k" * (MAX_KEY_LENGTH + 1)
    resp = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert key not in resp.text, "the rejected key was echoed back"
    assert job_count(migrated_engine) == 0


def test_key_at_exactly_255_chars_is_accepted(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """The cap is inclusive; an off-by-one here would reject a legal key."""
    key = "k" * MAX_KEY_LENGTH
    first = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})
    assert first.status_code == 202
    assert stored_key(migrated_engine, first.json()["id"]) == key

    second = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert job_count(migrated_engine) == 1


def test_whitespace_padded_key_is_the_same_key(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """``" abc "`` and ``"abc"`` must not be two jobs that look identical to a client."""
    first = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": "padded-key"})
    assert first.status_code == 202
    assert stored_key(migrated_engine, first.json()["id"]) == "padded-key"

    second = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": "  padded-key  "})
    assert second.status_code == 202
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json()["id"] == first.json()["id"]
    assert job_count(migrated_engine) == 1


def test_a_whitespace_only_key_is_treated_as_absent(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """An all-whitespace header carries no identity, so it must not become a shared key."""
    first = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": "   "})
    second = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": "   "})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    assert job_count(migrated_engine) == 2
    assert keys_stored(migrated_engine) == 0, "an all-whitespace key was stored"


# ---------------------------------------------------------------------------
# Concurrency — the gate
# ---------------------------------------------------------------------------


def test_concurrent_submissions_of_one_key_create_one_job(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """Eight threads, one key, one job.

    Real threads against the HTTP surface, released together by a barrier: a
    sequential loop would pass against a read-then-write implementation and prove
    nothing. ``Idempotency-Replayed`` is counted as well, because one row plus eight
    "created" answers would mean seven writes were lost rather than deduplicated.
    """
    key = f"concurrent-{uuid.uuid4()}"
    thread_count = 8
    barrier = threading.Barrier(thread_count)
    created_ids: list[str] = [""] * thread_count
    replayed_flags: list[bool] = [False] * thread_count
    errors: list[BaseException] = []

    def _submit(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            response = db_client.post("/v1/jobs", json=body(), headers={"Idempotency-Key": key})
            assert response.status_code == 202, response.text
            created_ids[index] = response.json()["id"]
            replayed_flags[index] = response.headers.get("Idempotency-Replayed") == "true"
        except BaseException as error:  # noqa: BLE001 - re-raised on the main thread
            errors.append(error)

    threads = [
        threading.Thread(target=_submit, args=(index,), daemon=True)
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a submitting thread did not finish"

    assert errors == [], f"a concurrent submission failed: {errors}"
    assert len(set(created_ids)) == 1, f"the key produced several job ids: {set(created_ids)}"
    assert job_count(migrated_engine) == 1, "the key produced several rows"
    assert replayed_flags.count(False) == 1, (
        f"exactly one submission must report a create, got {replayed_flags.count(False)}"
    )


def test_concurrent_submissions_with_different_keys_create_one_job_each(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """The other side of the race: distinct keys must not serialise into one row."""
    thread_count = 8
    barrier = threading.Barrier(thread_count)
    created_ids: list[str] = [""] * thread_count
    errors: list[BaseException] = []

    def _submit(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            response = db_client.post(
                "/v1/jobs",
                json=body(),
                headers={"Idempotency-Key": f"distinct-{uuid.uuid4()}-{index}"},
            )
            assert response.status_code == 202, response.text
            created_ids[index] = response.json()["id"]
        except BaseException as error:  # noqa: BLE001 - re-raised on the main thread
            errors.append(error)

    threads = [
        threading.Thread(target=_submit, args=(index,), daemon=True)
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a submitting thread did not finish"

    assert errors == [], f"a concurrent submission failed: {errors}"
    assert len(set(created_ids)) == thread_count
    assert job_count(migrated_engine) == thread_count


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("idempotency_key", "request_fingerprint", "label"),
    [
        ("key-without-fingerprint", None, "key without a fingerprint"),
        (None, "fingerprint-without-key", "fingerprint without a key"),
    ],
)
def test_ck_jobs_idempotency_pair_rejects_a_half_set_pair(
    migrated_engine: Engine,
    idempotency_key: str | None,
    request_fingerprint: str | None,
    label: str,
) -> None:
    """Half a pair is unusable: uncompareable on replay, or unlookupable."""
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, type, payload, idempotency_key, request_fingerprint) "
                    "VALUES (:id, 'pair.test', '{}'::jsonb, :key, :fingerprint)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "key": idempotency_key,
                    "fingerprint": request_fingerprint,
                },
            )
            conn.commit()


def test_a_second_row_with_the_same_key_is_rejected(migrated_engine: Engine) -> None:
    """The unique index is the race guarantee; assert it directly, not only via HTTP."""
    key = "duplicate-key-9f2a"
    fingerprint = "a" * 64

    with migrated_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, type, payload, idempotency_key, request_fingerprint) "
                "VALUES (:id, 'dup.test', '{}'::jsonb, :key, :fingerprint)"
            ),
            {"id": str(uuid.uuid4()), "key": key, "fingerprint": fingerprint},
        )
        conn.commit()

    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, idempotency_key, request_fingerprint) "
                    "VALUES (:id, 'dup.test', '{}'::jsonb, :key, :fingerprint)"
                ),
                {"id": str(uuid.uuid4()), "key": key, "fingerprint": fingerprint},
            )
            conn.commit()


def test_many_rows_without_a_key_are_allowed(migrated_engine: Engine) -> None:
    """A unique index over a nullable column would make "no key" one shared key."""
    with migrated_engine.connect() as conn:
        for _ in range(3):
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload) VALUES (:id, 'nokey.test', '{}'::jsonb)"
                ),
                {"id": str(uuid.uuid4())},
            )
        conn.commit()

    assert job_count(migrated_engine) == 3


# ---------------------------------------------------------------------------
# Fingerprint definition
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_and_order_independent() -> None:
    """The single definition the router relies on: same request, same digest."""
    from app import service

    assert service.request_fingerprint("echo", {"a": 2, "b": 1}, 3) == (
        service.request_fingerprint("echo", {"b": 1, "a": 2}, 3)
    )
    assert service.request_fingerprint("echo", {"a": 1}, 3) != (
        service.request_fingerprint("echo", {"a": 2}, 3)
    )
    assert service.request_fingerprint("echo", {}, 3) != (
        service.request_fingerprint("echo", {}, 5)
    )
    assert len(service.request_fingerprint("echo", {}, 3)) == 64


def test_fingerprint_matches_the_documented_canonical_form() -> None:
    """Pinning the digest keeps a future canonicalisation change from being silent.

    A changed canonical form would invalidate every key already stored under the old
    one, so it has to be a deliberate, visible edit.
    """
    import hashlib

    from app import service

    canonical = json.dumps(
        {"type": "echo", "payload": {"b": 1, "a": 2}, "max_attempts": 3},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected = hashlib.sha256(canonical.encode()).hexdigest()
    assert service.request_fingerprint("echo", {"a": 2, "b": 1}, 3) == expected
