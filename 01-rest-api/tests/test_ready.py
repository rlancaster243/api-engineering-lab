"""Readiness endpoint (PR-08): liveness vs persistence availability."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ready import database_is_ready
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


def test_ready_performs_a_query_not_a_pool_heuristic(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate E regression guard: readiness must not trust pool metadata alone."""
    calls: list[str] = []
    original = database_is_ready

    def instrumented(session: Session) -> None:
        calls.append("probe")
        original(session)

    monkeypatch.setattr("app.main.database_is_ready", instrumented)
    assert api.get("/ready").status_code == 200
    assert calls == ["probe"]


def test_ready_rejects_post(api: TestClient) -> None:
    assert api.post("/ready").status_code == 405
