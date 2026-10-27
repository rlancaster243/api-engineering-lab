"""Readiness endpoint (PR-08): liveness vs persistence availability."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import create_session_factory
from tests.conftest import MintToken


def test_ready_returns_ok_when_database_responds(api: TestClient) -> None:
    response = api.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_does_not_require_authentication(api_jwt: tuple[TestClient, MintToken]) -> None:
    client, _ = api_jwt
    assert client.get("/ready").status_code == 200


def test_ready_returns_503_when_database_is_unavailable(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_check(session: Session) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr("app.main.database_is_ready", fail_check)
    response = api.get("/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_ready_returns_503_against_a_real_unreachable_database(api: TestClient) -> None:
    """No patching of the probe: a dead database must surface as 503, sanitized."""
    original_factory = api.app.state.session_factory
    dead_engine = create_engine(
        "postgresql+psycopg://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    try:
        api.app.state.session_factory = create_session_factory(dead_engine)
        response = api.get("/ready")
    finally:
        api.app.state.session_factory = original_factory
        dead_engine.dispose()
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["request_id"]
    body = response.text.lower()
    for leaked in ("127.0.0.1", "psycopg", "connection", "refused"):
        assert leaked not in body


def test_ready_executes_select_1_on_the_apps_engine(api: TestClient) -> None:
    """Readiness must run SQL; pool metadata alone cannot satisfy this."""
    engine = api.app.state.session_factory.kw["bind"]
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = api.get("/ready")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert any(s.strip().upper() == "SELECT 1" for s in statements), statements


def test_ready_rejects_post(api: TestClient) -> None:
    assert api.post("/ready").status_code == 405
