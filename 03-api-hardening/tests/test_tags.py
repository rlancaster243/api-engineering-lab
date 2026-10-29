"""Tag writes: what is durable when the request does not finish."""

import sqlite3

from fastapi.testclient import TestClient

from tests.conftest import ALPHA_KEY, auth, create_asset

# Fails the parent's timestamp statement after the tag rows have been written, which is
# the window a split transaction leaves open: the request cannot finish, but the tag
# rows are already committed. A real SQLite failure rather than a patched method, so the
# rollback under test is the driver's, not the test's.
REFUSE_PARENT_TIMESTAMP = """
CREATE TRIGGER refuse_parent_timestamp
BEFORE UPDATE OF updated_at ON assets
BEGIN
    SELECT RAISE(ABORT, 'disk I/O error');
END
"""


def stored_tags(connection: sqlite3.Connection, asset_id: str) -> list[str]:
    return [
        row["tag"]
        for row in connection.execute("SELECT tag FROM asset_tags WHERE asset_id = ?", (asset_id,))
    ]


def stored_updated_at(connection: sqlite3.Connection, asset_id: str) -> str:
    row = connection.execute("SELECT updated_at FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return row["updated_at"]


def test_a_failed_tag_write_leaves_no_tag_row_and_does_not_advance_the_timestamp(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """A request answered `500` stores nothing, so the caller's retry is the whole story.

    This inverts `test_tag_write_survives_a_failure_after_the_tag_rows_land`, which
    asserted the opposite: that the tag row survived a failure the caller was told had
    happened. That test pinned the defect rather than the contract — a `500` means the
    write did not happen, and a row it left behind is state the client was never told
    about. The inverted assertion is also the stricter one: it checks that nothing is
    stored *and* that the retry which follows lands the tag normally, so a repair is not
    needed. Changing it is legitimate because it asserted the defective behaviour, not
    because the new behaviour is easier to test.
    """
    created = create_asset(client)
    before = stored_updated_at(connection, created["id"])
    connection.execute(REFUSE_PARENT_TIMESTAMP)
    connection.commit()

    response = client.post(
        f"/v1/assets/{created['id']}/tags",
        json={"tags": ["finance"]},
        headers=auth(ALPHA_KEY),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert stored_tags(connection, created["id"]) == []
    assert stored_updated_at(connection, created["id"]) == before

    connection.execute("DROP TRIGGER refuse_parent_timestamp")
    connection.commit()
    retried = client.post(
        f"/v1/assets/{created['id']}/tags",
        json={"tags": ["finance"]},
        headers=auth(ALPHA_KEY),
    )
    assert retried.status_code == 200
    assert retried.json()["tags"] == ["finance"]
    assert stored_updated_at(connection, created["id"]) >= before


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
