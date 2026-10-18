import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session

from alembic import command
from app import db
from app.models import DEV_USER_EMAIL, DEV_USER_ID, SavedItem, User


def test_create_get_update_delete(client: TestClient) -> None:
    created = client.post("/v1/items", json={"content_id": "verse-1", "notes": "first"})
    assert created.status_code == 201
    body = created.json()
    item_id = body["id"]
    assert body["content_id"] == "verse-1"
    assert body["notes"] == "first"
    assert "user_id" not in body

    fetched = client.get(f"/v1/items/{item_id}")
    assert fetched.status_code == 200
    assert fetched.json()["content_id"] == "verse-1"

    patched = client.patch(f"/v1/items/{item_id}", json={"notes": "revised"})
    assert patched.status_code == 200
    assert patched.json()["notes"] == "revised"
    assert patched.json()["content_id"] == "verse-1"

    deleted = client.delete(f"/v1/items/{item_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/v1/items/{item_id}").status_code == 404


def test_missing_item_and_invalid_input(client: TestClient) -> None:
    missing_id = uuid.uuid4()
    assert client.get(f"/v1/items/{missing_id}").status_code == 404
    assert client.patch(f"/v1/items/{missing_id}", json={"notes": "x"}).status_code == 404
    assert client.delete(f"/v1/items/{missing_id}").status_code == 404

    assert client.post("/v1/items", json={}).status_code == 422
    assert client.post("/v1/items", json={"content_id": ""}).status_code == 422
    rejected = client.post(
        "/v1/items",
        json={"content_id": "ok", "user_id": str(DEV_USER_ID)},
    )
    assert rejected.status_code == 422
    rejected_patch = client.patch(
        f"/v1/items/{missing_id}",
        json={"content_id": "changed"},
    )
    assert rejected_patch.status_code == 422
    assert client.get("/v1/items/not-a-uuid").status_code == 422


def test_duplicate_save_is_conflict(client: TestClient, db_session: Session) -> None:
    first = client.post("/v1/items", json={"content_id": "shared-content"})
    assert first.status_code == 201
    second = client.post("/v1/items", json={"content_id": "shared-content", "notes": "again"})
    assert second.status_code == 409
    count = db_session.scalar(select(func.count()).select_from(SavedItem))
    assert count == 1


def test_different_owners_can_save_same_content(db_session: Session) -> None:
    other = User(id=uuid.uuid4(), email="other@localhost")
    db_session.add(other)
    db_session.flush()
    db_session.add_all(
        [
            SavedItem(
                id=uuid.uuid4(),
                user_id=DEV_USER_ID,
                content_id="same-content",
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
            SavedItem(
                id=uuid.uuid4(),
                user_id=other.id,
                content_id="same-content",
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()
    count = db_session.scalar(
        select(func.count()).select_from(SavedItem).where(SavedItem.content_id == "same-content")
    )
    assert count == 2


def test_list_order_and_pagination(client: TestClient, db_session: Session) -> None:
    created_ids: list[str] = []
    for content_id in ("older", "middle", "newer"):
        response = client.post("/v1/items", json={"content_id": content_id})
        assert response.status_code == 201
        created_ids.append(response.json()["id"])

    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index, item_id in enumerate(created_ids):
        db_session.execute(
            update(SavedItem)
            .where(SavedItem.id == uuid.UUID(item_id))
            .values(created_at=base + timedelta(minutes=index))
        )
    db_session.commit()

    first_page = client.get("/v1/items", params={"limit": 2, "offset": 0})
    assert first_page.status_code == 200
    assert first_page.json()["limit"] == 2
    assert first_page.json()["offset"] == 0
    assert [item["content_id"] for item in first_page.json()["items"]] == ["newer", "middle"]

    second_page = client.get("/v1/items", params={"limit": 2, "offset": 2})
    assert [item["content_id"] for item in second_page.json()["items"]] == ["older"]

    assert client.get("/v1/items", params={"limit": 0}).status_code == 422
    assert client.get("/v1/items", params={"limit": 101}).status_code == 422
    assert client.get("/v1/items", params={"offset": -1}).status_code == 422


def test_rollback_does_not_persist(db_session: Session) -> None:
    item = SavedItem(
        id=uuid.uuid4(),
        user_id=DEV_USER_ID,
        content_id="rolled-back",
        notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(item)
    db_session.flush()
    db_session.rollback()
    count = db_session.scalar(
        select(func.count()).select_from(SavedItem).where(SavedItem.content_id == "rolled-back")
    )
    assert count == 0


def test_item_survives_engine_reset(client: TestClient) -> None:
    created = client.post("/v1/items", json={"content_id": "durable"})
    assert created.status_code == 201
    item_id = created.json()["id"]
    db.reset_engine()
    fetched = client.get(f"/v1/items/{item_id}")
    assert fetched.status_code == 200
    assert fetched.json()["content_id"] == "durable"


def test_migration_upgrade_from_clean_database(database_url: str, migrated_database: str) -> None:
    assert migrated_database == database_url
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.downgrade(config, "base")
    try:
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        with engine.connect() as connection:
            email = connection.execute(
                text("SELECT email FROM users WHERE id = :id"),
                {"id": DEV_USER_ID},
            ).scalar_one()
            item_count = connection.execute(text("SELECT count(*) FROM saved_items")).scalar_one()
        engine.dispose()
        assert email == DEV_USER_EMAIL
        assert item_count == 0
    finally:
        command.upgrade(config, "head")
