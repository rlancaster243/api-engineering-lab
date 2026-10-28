"""Batch reprocessing."""

from fastapi.testclient import TestClient

from tests.conftest import ALPHA_KEY, BETA_KEY, auth, create_asset


def test_reprocess_derives_a_new_fingerprint_for_every_owned_asset(client: TestClient) -> None:
    first = create_asset(client, ALPHA_KEY, name="first")
    second = create_asset(client, ALPHA_KEY, name="second")

    response = client.post(
        "/v1/batch/reprocess",
        json={"ids": [first["id"], second["id"]]},
        headers=auth(ALPHA_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["processed"] == 2
    assert body["failed"] == 0
    assert [item["status"] for item in body["results"]] == ["reprocessed", "reprocessed"]

    reread = client.get(f"/v1/assets/{first['id']}", headers=auth(ALPHA_KEY)).json()
    assert reread["metadata_version"] == 2


def test_reprocess_reports_an_empty_batch_as_invalid(client: TestClient) -> None:
    response = client.post("/v1/batch/reprocess", json={"ids": []}, headers=auth(ALPHA_KEY))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reprocess_requires_an_api_key(client: TestClient) -> None:
    response = client.post("/v1/batch/reprocess", json={"ids": ["anything"]})
    assert response.status_code == 401


def test_reprocess_returns_200_for_a_batch_that_changed_nothing(client: TestClient) -> None:
    """Ids the caller cannot see are reported item by item rather than as a request error."""
    unknown = "3f1b0f4e-0000-4000-8000-000000000000"
    response = client.post(
        "/v1/batch/reprocess",
        json={"ids": [unknown, unknown]},
        headers=auth(BETA_KEY),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["processed"] == 0
    assert body["failed"] == 2
    assert {item["reason"] for item in body["results"]} == {"not_found"}
