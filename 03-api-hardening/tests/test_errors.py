"""Error envelope: shape, status codes and what a failure says to the caller."""

import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ALPHA_KEY, auth, create_asset

INTERNAL_MESSAGE = "An unexpected error occurred."
UNAVAILABLE_MESSAGE = "The service is temporarily unavailable."


def test_unknown_route_uses_the_envelope(client: TestClient) -> None:
    response = client.get("/v1/nothing-here", headers=auth(ALPHA_KEY))
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "ROUTE_NOT_FOUND"
    assert error["request_id"]
    assert response.headers["X-Request-ID"] == error["request_id"]


def test_wrong_method_uses_the_envelope_and_keeps_allow(client: TestClient) -> None:
    response = client.put("/v1/assets", json={}, headers=auth(ALPHA_KEY))
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "POST" in response.headers["Allow"]


def test_every_error_carries_a_distinct_request_id(client: TestClient) -> None:
    first = client.get(f"/v1/assets/{uuid.uuid4()}", headers=auth(ALPHA_KEY))
    second = client.get(f"/v1/assets/{uuid.uuid4()}", headers=auth(ALPHA_KEY))
    assert first.json()["error"]["request_id"] != second.json()["error"]["request_id"]


def test_client_supplied_request_id_is_not_trusted(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "attacker-controlled-string"})
    assert response.headers["X-Request-ID"] != "attacker-controlled-string"


def test_a_storage_failure_does_not_quote_the_driver(client: TestClient, connection) -> None:
    """A `500` body carries the contract's fixed message, not the driver's text.

    This inverts the previous assertion deliberately. The old test pinned the leak: it
    required the missing relation to appear in the response so an unmigrated deployment
    would be diagnosable from the body. The caller is not the operator, and the diagnostic
    was never only in the body — the exception is logged with `exc_info` on a line that
    carries the same `request_id` the response returns, which is how a support report is
    matched to the fault.
    """
    created = create_asset(client)
    connection.execute("DROP TABLE asset_tags")

    response = client.get(f"/v1/assets/{created['id']}", headers=auth(ALPHA_KEY))

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"] == INTERNAL_MESSAGE
    assert error["request_id"]
    assert "asset_tags" not in response.text


def test_contention_is_unavailable_without_the_driver_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-fault `sqlite3.Error` path answers 503 and also says nothing specific."""

    def locked(self: object, tenant_id: str, limit: int) -> list[sqlite3.Row]:
        raise sqlite3.OperationalError("database is locked: /srv/private/registry.sqlite3")

    monkeypatch.setattr("app.repository.AssetRepository.list_for_tenant", locked)

    response = client.get("/v1/assets", headers=auth(ALPHA_KEY))

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["message"] == UNAVAILABLE_MESSAGE
    assert "/srv/private" not in response.text


def test_a_malformed_identifier_does_not_reach_the_database(client: TestClient) -> None:
    response = client.post(
        "/v1/batch/reprocess",
        json={"ids": ["'; DROP TABLE assets; --"]},
        headers=auth(ALPHA_KEY),
    )
    # Every id fails here, so the batch reports 207 (no item was processed); the point of
    # the test is that the statement was bound, not concatenated.
    assert response.status_code == 207
    assert response.json()["failed"] == 1
    assert client.get("/v1/assets", headers=auth(ALPHA_KEY)).status_code == 200
