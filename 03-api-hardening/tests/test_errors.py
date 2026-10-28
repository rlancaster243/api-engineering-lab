"""Error envelope: shape, status codes and what a failure says to the caller."""

import uuid

from fastapi.testclient import TestClient

from tests.conftest import ALPHA_KEY, auth, create_asset


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


def test_a_database_failure_identifies_the_missing_relation(client: TestClient, connection) -> None:
    """A deployment pointed at an unmigrated file must be diagnosable from the response.

    The failing statement is the only thing that distinguishes "schema missing" from
    "storage corrupt" in a support report, so the server-side detail is returned rather
    than reduced to a generic message.
    """
    created = create_asset(client)
    connection.execute("DROP TABLE asset_tags")

    response = client.get(f"/v1/assets/{created['id']}", headers=auth(ALPHA_KEY))

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["request_id"]
    assert "asset_tags" in error["message"]


def test_a_malformed_identifier_does_not_reach_the_database(client: TestClient) -> None:
    response = client.post(
        "/v1/batch/reprocess",
        json={"ids": ["'; DROP TABLE assets; --"]},
        headers=auth(ALPHA_KEY),
    )
    assert response.status_code == 200
    assert response.json()["failed"] == 1
    assert client.get("/v1/assets", headers=auth(ALPHA_KEY)).status_code == 200
