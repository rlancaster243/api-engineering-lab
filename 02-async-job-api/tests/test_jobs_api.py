"""Tests for POST /v1/jobs and GET /v1/jobs/{id}.

Requires a PostgreSQL database via TEST_DATABASE_URL.
All tests use the migrated_engine and client fixtures from conftest.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


@pytest.fixture
def db_client(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient wired to the migrated test database, with a clean jobs table."""
    url = migrated_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    # Re-import app so lifespan picks up the monkeypatched DATABASE_URL.
    import importlib

    import app.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c
    # Clean up between tests.
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
    # Submitted value must not appear in details
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


# ---------------------------------------------------------------------------
# Falsification case 5: DB unavailable → 503, no row created
# ---------------------------------------------------------------------------


def test_db_unavailable_returns_503_not_202(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With engine pointed at a dead port, POST returns 503, not 202."""
    dead_url = "postgresql://postgres:postgres@127.0.0.1:19999/nonexistent"
    monkeypatch.setenv("DATABASE_URL", dead_url)

    import importlib

    import app.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        resp = c.post("/v1/jobs", json={"type": "echo", "payload": {}})

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"

    # Confirm the real DB has no new row from this failed attempt.
    with migrated_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# 405 wrong method
# ---------------------------------------------------------------------------


def test_wrong_method_returns_405_envelope(db_client: TestClient) -> None:
    resp = db_client.delete("/v1/jobs")
    assert resp.status_code == 405
    error = resp.json()["error"]
    assert error["code"] == "METHOD_NOT_ALLOWED"
    assert "request_id" in error
