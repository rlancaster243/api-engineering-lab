"""Tests for POST /v1/jobs and GET /v1/jobs/{id}.

Requires a PostgreSQL database via TEST_DATABASE_URL.
All tests use the migrated_engine and client fixtures from conftest.py.

db_client wiring: monkeypatch sets DATABASE_URL to the test URL before entering
TestClient. The app lifespan calls load_settings() at startup which reads
os.environ, so it picks up the patched value without any module reload. The app
module is imported once; middleware and handlers are registered once.
"""

import asyncio
import io
import logging
import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.errors import _handle_dbapi_error
from app.logging import stack_only


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
# A legacy non-object result must not break GET
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("jsonb_literal", "type_name"),
    [
        ("'[1, 2, 3]'::jsonb", "array"),
        ("'\"just a string\"'::jsonb", "string"),
        ("'7'::jsonb", "number"),
        ("'null'::jsonb", "null"),
        ("'true'::jsonb", "boolean"),
    ],
)
def test_get_job_returns_200_for_a_non_object_result(
    db_client: TestClient, migrated_engine: Engine, jsonb_literal: str, type_name: str
) -> None:
    """A row written before the worker guarded its result must not 500 the endpoint.

    Seeded by direct SQL: the worker now refuses to store a non-object result, so
    this state can only come from data written earlier.
    """
    job_id = uuid.uuid4()
    with migrated_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, type, payload, status, result, finished_at) "
                "VALUES (:id, 'legacy.result', '{}'::jsonb, 'SUCCEEDED', "
                f"{jsonb_literal}, now())"
            ),
            {"id": job_id},
        )
        stored_type = conn.execute(
            text("SELECT jsonb_typeof(result) FROM jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()
        conn.commit()
    assert stored_type == type_name, f"seed did not produce a {type_name} result"

    resp = db_client.get(f"/v1/jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCEEDED"
    assert body["result"] is None, "a non-object result is served as null"
    assert body["id"] == str(job_id)


def test_get_job_still_serves_a_well_formed_object_result(
    db_client: TestClient, migrated_engine: Engine
) -> None:
    """The object-or-null contract is unchanged for rows the worker wrote."""
    job_id = uuid.uuid4()
    with migrated_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, type, payload, status, result, finished_at) "
                "VALUES (:id, 'legacy.result', '{}'::jsonb, 'SUCCEEDED', "
                '\'{"echoed": {"m": 1}}\'::jsonb, now())'
            ),
            {"id": job_id},
        )
        conn.commit()

    resp = db_client.get(f"/v1/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["result"] == {"echoed": {"m": 1}}


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


# ---------------------------------------------------------------------------
# Database failures must not echo the submitted row into the logs
# ---------------------------------------------------------------------------


def test_database_failure_logging_omits_the_submitted_row(
    migrated_engine: Engine,
) -> None:
    """psycopg's ``DETAIL: Failing row contains (...)`` must not reach a log line.

    The submitted payload is in that detail, so a logged traceback would put the
    request body on stdout. SQLSTATE and the call sites are kept for diagnosis.
    """
    secret = "logged-row-secret-7c31"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    errors_logger = logging.getLogger("app.errors")
    errors_logger.addHandler(handler)
    previous_level = errors_logger.level
    errors_logger.setLevel(logging.DEBUG)
    try:
        with migrated_engine.connect() as conn:
            try:
                conn.execute(
                    text(
                        "INSERT INTO jobs (id, type, payload) "
                        "VALUES (:id, 'Bad-Type!', CAST(:payload AS jsonb))"
                    ),
                    {"id": uuid.uuid4(), "payload": f'{{"api_key": "{secret}"}}'},
                )
                conn.commit()
            except IntegrityError as error:
                response = asyncio.run(_handle_dbapi_error(_stub_request(), error))
    finally:
        errors_logger.removeHandler(handler)
        errors_logger.setLevel(previous_level)

    assert response.status_code == 500
    output = stream.getvalue()
    assert secret not in output, "the submitted row reached the logs"
    assert "Failing row" not in output
    assert "23514" in output, "the SQLSTATE must survive for diagnosis"
    assert "_handle_dbapi_error" in output or "test_jobs_api" in output


def _stub_request() -> Any:
    class _State:
        request_id = "stub-request-id"

    class _Request:
        state = _State()

    return _Request()


def test_stack_only_keeps_frames_and_drops_the_message() -> None:
    """The diagnostic that replaces a traceback must name call sites, not values.

    ``format_tb`` renders source lines and never evaluates them, so a literal in the
    source can appear but the runtime value a message interpolates cannot.
    """
    secret = "stack-secret-1188"

    def _raise(api_key: str) -> None:
        raise RuntimeError(f"rejected key {api_key}")

    try:
        _raise(secret)
    except RuntimeError as error:
        rendered = stack_only(error)

    assert secret not in rendered, "the runtime value must not reach the log"
    assert "_raise" in rendered, "the frame that raised must still be identifiable"
    assert "File " in rendered
