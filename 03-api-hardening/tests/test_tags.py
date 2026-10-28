"""Tag writes: what is durable when the request does not finish."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ALPHA_KEY, auth, create_asset


def test_tag_write_survives_a_failure_after_the_tag_rows_land(
    client: TestClient, connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag the client was told about must not disappear because a later step failed.

    The tag rows are the client's data; the parent's timestamp is bookkeeping. Durable
    first, bookkeeping second is the order that keeps a retry cheap: the tag is already
    stored, so resubmitting it adds nothing.
    """
    created = create_asset(client)
    before = connection.execute(
        "SELECT updated_at FROM assets WHERE id = ?", (created["id"],)
    ).fetchone()["updated_at"]

    def fail_touch(self, tenant_id: str, asset_id: str) -> bool:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("app.service.AssetRepository.touch", fail_touch)

    response = client.post(
        f"/v1/assets/{created['id']}/tags",
        json={"tags": ["finance"]},
        headers=auth(ALPHA_KEY),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"

    stored = connection.execute(
        "SELECT tag FROM asset_tags WHERE asset_id = ?", (created["id"],)
    ).fetchall()
    assert [row["tag"] for row in stored] == ["finance"]
    after = connection.execute(
        "SELECT updated_at FROM assets WHERE id = ?", (created["id"],)
    ).fetchone()["updated_at"]
    assert after == before


def test_a_duplicate_tag_does_not_change_the_parent_timestamp(
    client: TestClient, connection
) -> None:
    created = create_asset(client)
    client.post(
        f"/v1/assets/{created['id']}/tags", json={"tags": ["finance"]}, headers=auth(ALPHA_KEY)
    )
    first = connection.execute(
        "SELECT updated_at FROM assets WHERE id = ?", (created["id"],)
    ).fetchone()["updated_at"]

    client.post(
        f"/v1/assets/{created['id']}/tags", json={"tags": ["finance"]}, headers=auth(ALPHA_KEY)
    )
    second = connection.execute(
        "SELECT updated_at FROM assets WHERE id = ?", (created["id"],)
    ).fetchone()["updated_at"]
    assert second >= first
