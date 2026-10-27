"""Tests for POST /v1/jobs and GET /v1/jobs/{id}.

Requires a PostgreSQL database via TEST_DATABASE_URL.
All tests use the migrated_engine and client fixtures from conftest.py.

db_client wiring: monkeypatch sets DATABASE_URL to the test URL before entering
TestClient. The app lifespan calls load_settings() at startup which reads
os.environ, so it picks up the patched value without any module reload. The app
module is imported once; middleware and handlers are registered once.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


@pytest.fixture
def db_client(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    """TestClient wired to the migrated test database, with a clean jobs table after each test."""
    url = migrated_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    from app.main import app

    with TestClient(app) as c:
        yield c
    with migrated_engine.connect() as conn:
        conn.execute(text("DELETE FROM jobs"))
        conn.commit()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_submit_returns_202_and_location(db_client: TestClient) -> None:
    resp = db_client.post("/v1/jobs", json={"type": "echo", "payload": {"msg": "hello"}})
    assert resp.status_code == 202
    assert "Location" in resp.headers
    data = resp.json()
    assert data["status"] == "PENDING"
    assert "id" in data
    assert "created_at" in data


def test_location_resolves_to_pending_job(db_client: TestClient) -> None:
    post = db_client.post("/v1/jobs", json={"type": "echo", "payload": {"k": 1}})
    assert post.status_code == 202
    job_id = post.json()["id"]
    location = post.headers["Location"]
    assert location.endswith(f"/v1/jobs/{job_id}")

    get = db_client.get(f"/v1/jobs/{job_id}")
    assert get.status_code == 200
    body = get.json()
    assert body["status"] == "PENDING"
    assert body["attempt"] == 0
    assert body["type"] == "echo"


def test_durability_before_acceptance(db_client: TestClient, migrated_engine: Engine) -> None:
    """A separate engine connection sees the row after the 202, proving commit preceded response."""
    resp = db_client.post("/v1/jobs", json={"type": "sync.check", "payload": {"x": 1}})
    assert resp.status_code == 202
    job_id = resp.json()["id"]

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, status FROM jobs WHERE id = :id"), {"id": job_id}
        ).one_or_none()
    assert row is not None, "Row must be visible via independent connection after 202"
    assert row.status == "PENDING"


def test_max_attempts_stored(db_client: TestClient) -> None:
    resp = db_client.post("/v1/jobs", json={"type": "echo", "payload": {"a": 1}, "max_attempts": 5})
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    get = db_client.get(f"/v1/jobs/{job_id}")
    assert get.json()["max_attempts"] == 5


# ---------------------------------------------------------------------------
# 404 path
# ---------------------------------------------------------------------------


def test_unknown_id_returns_404_envelope(db_client: TestClient) -> None:
    missing = str(uuid.uuid4())
    resp = db_client.get(f"/v1/jobs/{missing}")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "JOB_NOT_FOUND"
    assert "request_id" in error


# ---------------------------------------------------------------------------
# Validation (422)
# ---------------------------------------------------------------------------


def test_malformed_type_returns_422(db_client: TestClient) -> None:
    resp = db_client.post("/v1/jobs", json={"type": "Bad-Type!", "payload": {}})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    raw = str(resp.json())
    assert "Bad-Type!" not in raw


def test_non_object_payload_returns_422(db_client: TestClient) -> None:
    resp = db_client.post("/v1/jobs", json={"type": "echo", "payload": [1, 2, 3]})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    raw = str(resp.json())
    assert "[1, 2, 3]" not in raw


def test_max_attempts_below_1_returns_422(db_client: TestClient) -> None:
    resp = db_client.post("/v1/jobs", json={"type": "echo", "payload": {}, "max_attempts": 0})
    assert resp.status_code == 422


def test_max_attempts_above_20_returns_422(db_client: TestClient) -> None:
    resp = db_client.post("/v1/jobs", json={"type": "echo", "payload": {}, "max_attempts": 21})
    assert resp.status_code == 422


def test_payload_at_size_limit_accepted(db_client: TestClient) -> None:
    """Payload of exactly 65,536 bytes (the cap) must be accepted.

    json.dumps({"a": "x"*65527}) = 65536 bytes (> check is strict).
    """
    resp = db_client.post("/v1/jobs", json={"type": "echo", "payload": {"a": "x" * 65527}})
    assert resp.status_code == 202


def test_payload_one_byte_over_limit_rejected(db_client: TestClient) -> None:
    """Payload of exactly 65,537 bytes must be rejected with VALIDATION_ERROR.

    json.dumps({"a": "x"*65528}) = 65537 bytes.
    The oversized body must not appear in the response.
    """
    resp = db_client.post("/v1/jobs", json={"type": "echo", "payload": {"a": "x" * 65528}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "x" * 65528 not in resp.text


def test_nan_in_payload_returns_422(db_client: TestClient) -> None:
    """NaN in payload must return 422, not a 500 from the JSONB adapter.

    httpx's json= helper does not allow NaN, so a raw body is required.
    """
    resp = db_client.post(
        "/v1/jobs",
        content=b'{"type": "echo", "payload": {"x": NaN}}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Falsification case 5: DB unavailable → 503, no row created
# ---------------------------------------------------------------------------


def test_db_unavailable_returns_503_not_202(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With engine pointed at a dead port, POST returns 503, not 202.

    Row count is measured before and after to avoid dependency on test ordering.
    DATABASE_URL is monkeypatched to a dead address; the lifespan picks it up at
    TestClient startup — no module reload needed.
    """
    with migrated_engine.connect() as conn:
        baseline = conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()

    dead_url = "postgresql://postgres:postgres@127.0.0.1:19999/nonexistent"
    monkeypatch.setenv("DATABASE_URL", dead_url)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post("/v1/jobs", json={"type": "echo", "payload": {}})

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"

    with migrated_engine.connect() as conn:
        after = conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()
    assert after == baseline, f"Expected row count unchanged ({baseline}), got {after}"


# ---------------------------------------------------------------------------
# 405 wrong method
# ---------------------------------------------------------------------------


def test_wrong_method_returns_405_envelope(db_client: TestClient) -> None:
    resp = db_client.delete("/v1/jobs")
    assert resp.status_code == 405
    error = resp.json()["error"]
    assert error["code"] == "METHOD_NOT_ALLOWED"
    assert "request_id" in error
