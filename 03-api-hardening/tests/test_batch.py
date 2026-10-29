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


def test_reprocess_answers_207_when_every_item_failed(client: TestClient) -> None:
    """A batch that changed nothing is not a success.

    This replaces `test_reprocess_returns_200_for_a_batch_that_changed_nothing`, which
    pinned the defect: it asserted `200` for a batch whose every id was unknown. `207`
    is the deliberate choice here and this test is the record of it.

    `503` was rejected because neither condition it describes holds: the request is well
    formed and the server is healthy, so `503` mislabels a client-side outcome as an
    outage and invites a retry that will fail identically. `ApiError` has no details
    channel, so raising would also discard the per-item `results` a caller needs to learn
    *why* each id failed. `207` keeps the body and marks the response as mixed.
    """
    unknown = "3f1b0f4e-0000-4000-8000-000000000000"
    response = client.post(
        "/v1/batch/reprocess",
        json={"ids": [unknown, unknown]},
        headers=auth(BETA_KEY),
    )
    assert response.status_code == 207
    body = response.json()
    assert body["processed"] == 0
    assert body["failed"] == 2
    assert {item["reason"] for item in body["results"]} == {"not_found"}
    assert len(body["results"]) == 2


def test_reprocess_answers_200_when_something_succeeded(client: TestClient) -> None:
    """Partial success stays `200`: per-item results carry the failure detail."""
    created = create_asset(client, ALPHA_KEY)
    unknown = "3f1b0f4e-0000-4000-8000-000000000000"

    response = client.post(
        "/v1/batch/reprocess",
        json={"ids": [created["id"], unknown]},
        headers=auth(ALPHA_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["processed"] == 1
    assert body["failed"] == 1
    assert [item["status"] for item in body["results"]] == ["reprocessed", "skipped"]
