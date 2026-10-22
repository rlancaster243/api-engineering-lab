"""HTTP behavior of /v1/items against PostgreSQL. IDs refer to spec section 8."""

import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.models import DEV_USER_ID


def _count(engine: Engine, where: str = "true", **params: object) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text(f"SELECT count(*) FROM saved_items WHERE {where}"), params
        ).scalar_one()


def _other_users_item(engine: Engine, content_id: str = "theirs") -> uuid.UUID:
    """An item owned by someone else, inserted directly (Project 1A has one principal)."""
    other_user, item_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, issuer, subject) VALUES (:id, 'https://idp', :sub)"),
            {"id": other_user, "sub": str(other_user)},
        )
        connection.execute(
            text(
                "INSERT INTO saved_items (id, user_id, content_id, notes) "
                "VALUES (:id, :user_id, :content_id, 'private')"
            ),
            {"id": item_id, "user_id": other_user, "content_id": content_id},
        )
    return item_id


def _error_code(response) -> str:
    return response.json()["error"]["code"]


# --- lifecycle --------------------------------------------------------------------------


def test_f001_f017_create_read_update_delete(api: TestClient, clean_db: Engine) -> None:
    created = api.post(
        "/v1/items",
        json={"content_id": " article-042 ", "title": "T", "url": "https://example.org/a"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["content_id"] == "article-042"
    assert "user_id" not in body
    assert body["created_at"] == body["updated_at"]  # one statement, one now()
    assert _count(clean_db, "id = :id AND user_id = :u", id=body["id"], u=DEV_USER_ID) == 1

    item = f"/v1/items/{body['id']}"
    assert api.get(item).json() == body
    assert api.patch(item, json={"notes": "n"}).json()["notes"] == "n"

    deleted = api.delete(item)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert _count(clean_db) == 0
    assert _error_code(api.get(item)) == "ITEM_NOT_FOUND"
    repeat = api.delete(item)
    assert (repeat.status_code, _error_code(repeat)) == (404, "ITEM_NOT_FOUND")


def test_f006_missing_item_is_404_on_every_method(api: TestClient) -> None:
    item = f"/v1/items/{uuid.uuid4()}"
    for response in (api.get(item), api.patch(item, json={"notes": "x"}), api.delete(item)):
        assert (response.status_code, _error_code(response)) == (404, "ITEM_NOT_FOUND")


def test_item_survives_application_restart(api: TestClient, clean_db: Engine) -> None:
    from app.main import app

    item_id = api.post("/v1/items", json={"content_id": "durable"}).json()["id"]
    # A second client runs shutdown and startup again: new engine, new pool.
    with TestClient(app) as restarted:
        assert restarted.get(f"/v1/items/{item_id}").json()["content_id"] == "durable"


# --- duplicates -------------------------------------------------------------------------


def test_f002_duplicate_save_is_409_and_writes_nothing(api: TestClient, clean_db: Engine) -> None:
    assert api.post("/v1/items", json={"content_id": "dup"}).status_code == 201
    for variant in ("dup", "  dup\t"):  # equal after normalization
        response = api.post("/v1/items", json={"content_id": variant, "notes": "again"})
        assert (response.status_code, _error_code(response)) == (409, "ITEM_ALREADY_SAVED")
    assert _count(clean_db) == 1


def test_nfc_equivalent_content_ids_are_the_same_item(api: TestClient) -> None:
    assert api.post("/v1/items", json={"content_id": "café"}).status_code == 201
    assert api.post("/v1/items", json={"content_id": "café"}).status_code == 409


def test_content_ids_are_case_sensitive(api: TestClient) -> None:
    assert api.post("/v1/items", json={"content_id": "Case"}).status_code == 201
    assert api.post("/v1/items", json={"content_id": "case"}).status_code == 201


def test_f003_different_owners_may_save_the_same_content(api: TestClient, clean_db: Engine) -> None:
    _other_users_item(clean_db, content_id="shared")
    assert api.post("/v1/items", json={"content_id": "shared"}).status_code == 201
    assert _count(clean_db, "content_id = 'shared'") == 2


# --- ownership --------------------------------------------------------------------------


def test_f009_f010_f011_another_owners_item_is_invisible_and_untouchable(
    api: TestClient, clean_db: Engine
) -> None:
    theirs = f"/v1/items/{_other_users_item(clean_db)}"
    responses = [
        api.get(theirs),
        api.patch(theirs, json={"notes": "overwritten"}),
        api.delete(theirs),
    ]
    for response in responses:
        assert (response.status_code, _error_code(response)) == (404, "ITEM_NOT_FOUND")
        assert "private" not in response.text
    assert _count(clean_db, "notes = 'private'") == 1  # neither changed nor deleted
    assert api.get("/v1/items").json()["items"] == []


def test_f008_client_supplied_owner_is_rejected(api: TestClient, clean_db: Engine) -> None:
    response = api.post("/v1/items", json={"content_id": "x", "user_id": str(uuid.uuid4())})
    assert (response.status_code, _error_code(response)) == (422, "VALIDATION_ERROR")
    assert _count(clean_db) == 0


# --- PATCH semantics --------------------------------------------------------------------


def test_f029_noop_patch_keeps_updated_at(api: TestClient) -> None:
    item = api.post("/v1/items", json={"content_id": "a", "title": "T"}).json()
    time.sleep(0.01)
    same = api.patch(f"/v1/items/{item['id']}", json={"title": "T"})
    assert same.status_code == 200
    assert same.json() == item


def test_real_change_advances_updated_at(api: TestClient) -> None:
    item = api.post("/v1/items", json={"content_id": "a"}).json()
    time.sleep(0.01)
    changed = api.patch(f"/v1/items/{item['id']}", json={"notes": "n"}).json()
    assert changed["updated_at"] > item["updated_at"]
    assert changed["created_at"] == item["created_at"]


def test_f030_omitted_fields_are_kept_and_null_clears(api: TestClient) -> None:
    item = api.post(
        "/v1/items", json={"content_id": "a", "title": "T", "url": "https://x.org", "notes": "n"}
    ).json()
    path = f"/v1/items/{item['id']}"
    kept = api.patch(path, json={"notes": "m"}).json()
    assert (kept["title"], kept["url"], kept["notes"]) == ("T", "https://x.org", "m")
    cleared = api.patch(path, json={"title": None, "url": None}).json()
    assert (cleared["title"], cleared["url"], cleared["notes"]) == (None, None, "m")


def test_f018_empty_patch_is_rejected_without_a_write(api: TestClient) -> None:
    item = api.post("/v1/items", json={"content_id": "a"}).json()
    response = api.patch(f"/v1/items/{item['id']}", json={})
    assert (response.status_code, _error_code(response)) == (422, "VALIDATION_ERROR")
    assert api.get(f"/v1/items/{item['id']}").json() == item


# --- pagination -------------------------------------------------------------------------


def _traverse(api: TestClient, limit: int, max_pages: int = 200) -> list[str]:
    # Bounded: a cursor that repeats its boundary row must fail the test, not hang it.
    seen: list[str] = []
    cursor = None
    for _ in range(max_pages):
        params: dict[str, object] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        page = api.get("/v1/items", params=params).json()
        assert len(page["items"]) <= limit
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError(f"traversal did not end within {max_pages} pages")


def test_f019_equal_timestamps_are_ordered_by_id_without_gaps_or_repeats(
    api: TestClient, clean_db: Engine
) -> None:
    ids = sorted((uuid.uuid4() for _ in range(7)), reverse=True)
    with clean_db.begin() as connection:
        for index, item_id in enumerate(ids):
            connection.execute(
                text(
                    "INSERT INTO saved_items (id, user_id, content_id, created_at, updated_at) "
                    "VALUES (:id, :u, :c, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                ),
                {"id": item_id, "u": DEV_USER_ID, "c": f"same-{index}"},
            )
    for limit in (1, 2, 3, 7, 100):
        assert _traverse(api, limit) == [str(item_id) for item_id in ids]


def test_list_is_newest_first_and_ends_with_a_null_cursor(api: TestClient) -> None:
    created = [api.post("/v1/items", json={"content_id": f"c{i}"}).json()["id"] for i in range(5)]
    assert _traverse(api, 2) == list(reversed(created))
    last = api.get("/v1/items", params={"limit": 5}).json()
    assert last["next_cursor"] is None


def test_f021_tampered_cursor_is_400(api: TestClient) -> None:
    for content_id in ("a", "b"):
        api.post("/v1/items", json={"content_id": content_id})
    cursor = api.get("/v1/items", params={"limit": 1}).json()["next_cursor"]
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    for bad in (tampered, "garbage", cursor + "x"):
        response = api.get("/v1/items", params={"cursor": bad})
        assert (response.status_code, _error_code(response)) == (400, "INVALID_CURSOR")


def test_default_limit_is_25(api: TestClient) -> None:
    for i in range(26):
        api.post("/v1/items", json={"content_id": f"c{i}"})
    page = api.get("/v1/items").json()
    assert len(page["items"]) == 25
    assert page["next_cursor"] is not None
