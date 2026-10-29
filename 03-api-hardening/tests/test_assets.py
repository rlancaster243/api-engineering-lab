"""Asset creation, read, list and tagging over HTTP."""

import uuid

from fastapi.testclient import TestClient

from tests.conftest import (
    ALPHA_KEY,
    ALPHA_TENANT,
    ASSET_BODY,
    BETA_KEY,
    BETA_TENANT,
    auth,
    create_asset,
)


def test_create_asset_returns_the_stored_record(client: TestClient) -> None:
    response = client.post("/v1/assets", json=ASSET_BODY, headers=auth(ALPHA_KEY))
    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["name"] == ASSET_BODY["name"]
    assert body["source_uri"] == ASSET_BODY["source_uri"]
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == 4096
    assert body["metadata_version"] == 1
    assert body["tags"] == []
    assert len(body["checksum"]) == 64
    assert body["created_at"] == body["updated_at"]


def test_create_asset_rejects_a_relative_source_uri(client: TestClient) -> None:
    response = client.post(
        "/v1/assets", json=ASSET_BODY | {"source_uri": "/reports/q3.pdf"}, headers=auth(ALPHA_KEY)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_asset_rejects_a_blank_name(client: TestClient) -> None:
    response = client.post("/v1/assets", json=ASSET_BODY | {"name": "   "}, headers=auth(ALPHA_KEY))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_validation_errors_do_not_echo_submitted_values(client: TestClient) -> None:
    secret = "do-not-echo-this-value"
    response = client.post(
        "/v1/assets",
        json=ASSET_BODY | {"source_uri": secret},
        headers=auth(ALPHA_KEY),
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "source_uri" in response.json()["error"]["message"]


def test_create_asset_requires_an_api_key(client: TestClient) -> None:
    response = client.post("/v1/assets", json=ASSET_BODY)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "UNAUTHENTICATED"
    assert response.headers["WWW-Authenticate"] == "ApiKey"


def test_create_asset_rejects_an_unknown_api_key(client: TestClient) -> None:
    response = client.post("/v1/assets", json=ASSET_BODY, headers=auth("not-a-real-key"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_get_asset_returns_the_detail_view(client: TestClient) -> None:
    created = create_asset(client)
    response = client.get(f"/v1/assets/{created['id']}", headers=auth(ALPHA_KEY))
    assert response.status_code == 200
    assert response.json() == created


def test_get_unknown_asset_is_not_found(client: TestClient) -> None:
    response = client.get(f"/v1/assets/{uuid.uuid4()}", headers=auth(ALPHA_KEY))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ASSET_NOT_FOUND"


def test_get_asset_requires_an_api_key(client: TestClient) -> None:
    created = create_asset(client)
    assert client.get(f"/v1/assets/{created['id']}").status_code == 401


def test_get_of_an_unowned_asset_is_not_found(client: TestClient) -> None:
    """Following a forwarded link from another tenant must not disclose the asset."""
    theirs = create_asset(client, ALPHA_KEY, name="Alpha only")

    response = client.get(f"/v1/assets/{theirs['id']}", headers=auth(BETA_KEY))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ASSET_NOT_FOUND"
    assert theirs["source_uri"] not in response.text
    assert theirs["name"] not in response.text


def test_lists_are_tenant_scoped(client: TestClient) -> None:
    mine = create_asset(client, ALPHA_KEY, name="Alpha only")
    theirs = create_asset(client, BETA_KEY, name="Beta only")

    alpha_page = client.get("/v1/assets", headers=auth(ALPHA_KEY)).json()
    beta_page = client.get("/v1/assets", headers=auth(BETA_KEY)).json()

    assert [asset["id"] for asset in alpha_page["assets"]] == [mine["id"]]
    assert [asset["id"] for asset in beta_page["assets"]] == [theirs["id"]]


def test_list_applies_the_requested_limit(client: TestClient) -> None:
    first = create_asset(client, ALPHA_KEY, name="first")
    second = create_asset(client, ALPHA_KEY, name="second")

    response = client.get("/v1/assets", params={"limit": 1}, headers=auth(ALPHA_KEY))
    assert response.status_code == 200
    assert response.headers["X-Applied-Limit"] == "1"
    assert [asset["id"] for asset in response.json()["assets"]] == [second["id"]]
    assert second["id"] != first["id"]


def test_list_rejects_a_limit_outside_the_range(client: TestClient) -> None:
    response = client.get("/v1/assets", params={"limit": 0}, headers=auth(ALPHA_KEY))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_is_empty_for_a_tenant_without_assets(client: TestClient) -> None:
    response = client.get("/v1/assets", headers=auth(BETA_KEY))
    assert response.status_code == 200
    assert response.json() == {"assets": []}


def test_add_tags_returns_the_asset_with_its_tags_sorted(client: TestClient) -> None:
    created = create_asset(client)
    response = client.post(
        f"/v1/assets/{created['id']}/tags",
        json={"tags": ["finance", "quarterly", "finance"]},
        headers=auth(ALPHA_KEY),
    )
    assert response.status_code == 200
    assert response.json()["tags"] == ["finance", "quarterly"]


def test_add_tags_advances_the_parent_timestamp(client: TestClient, connection) -> None:
    created = create_asset(client)
    client.post(
        f"/v1/assets/{created['id']}/tags",
        json={"tags": ["finance"]},
        headers=auth(ALPHA_KEY),
    )
    row = connection.execute(
        "SELECT created_at, updated_at FROM assets WHERE id = ?", (created["id"],)
    ).fetchone()
    assert row["updated_at"] > row["created_at"]


def test_add_tags_is_idempotent(client: TestClient) -> None:
    created = create_asset(client)
    for _ in range(2):
        response = client.post(
            f"/v1/assets/{created['id']}/tags",
            json={"tags": ["finance"]},
            headers=auth(ALPHA_KEY),
        )
        assert response.status_code == 200
    assert response.json()["tags"] == ["finance"]


def test_add_tags_on_an_unowned_asset_is_not_found(client: TestClient) -> None:
    created = create_asset(client, ALPHA_KEY)
    response = client.post(
        f"/v1/assets/{created['id']}/tags",
        json={"tags": ["stolen"]},
        headers=auth(BETA_KEY),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ASSET_NOT_FOUND"


def test_add_tags_rejects_a_tag_with_spaces(client: TestClient) -> None:
    created = create_asset(client)
    response = client.post(
        f"/v1/assets/{created['id']}/tags",
        json={"tags": ["not a tag"]},
        headers=auth(ALPHA_KEY),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_assets_are_stored_with_their_owning_tenant(client: TestClient, connection) -> None:
    created = create_asset(client, BETA_KEY)
    row = connection.execute(
        "SELECT tenant_id FROM assets WHERE id = ?", (created["id"],)
    ).fetchone()
    assert row["tenant_id"] == BETA_TENANT
    assert ALPHA_TENANT != BETA_TENANT
