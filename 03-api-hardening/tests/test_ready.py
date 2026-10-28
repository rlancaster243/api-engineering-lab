"""Liveness and readiness."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ALPHA_KEY, auth


def test_ready_reports_ready(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_does_not_require_an_api_key(client: TestClient) -> None:
    assert client.get("/ready").status_code == 200


def test_ready_is_unavailable_when_the_file_cannot_be_opened(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("app.main.connect", refuse)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_health_does_not_touch_storage(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("app.main.connect", refuse)
    assert client.get("/health").status_code == 200


def test_assets_are_readable_through_a_second_connection(client: TestClient, database_path) -> None:
    """The request connection is not the only reader of the file."""
    from tests.conftest import create_asset

    created = create_asset(client, ALPHA_KEY)
    with sqlite3.connect(str(database_path)) as raw:
        stored = raw.execute("SELECT name FROM assets WHERE id = ?", (created["id"],)).fetchone()
    assert stored is not None
    assert stored[0] == created["name"]
    assert client.get(f"/v1/assets/{created['id']}", headers=auth(ALPHA_KEY)).status_code == 200
