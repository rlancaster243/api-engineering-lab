"""F-016: database unavailable maps to 503 with no false success (spec PR-07)."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from app.repository import ItemRepository
from app.users import UserRepository
from tests.conftest import MintToken


def _operational_error() -> OperationalError:
    return OperationalError("statement", {}, Exception("connection refused"))


def _count_items(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM saved_items")).scalar_one()


def test_f016_create_returns_503_and_does_not_persist(
    api: TestClient, clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_create(self, **kwargs: object) -> object:
        raise _operational_error()

    monkeypatch.setattr(ItemRepository, "create", fail_create)
    response = api.post("/v1/items", json={"content_id": "should-not-save"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert _count_items(clean_db) == 0


def test_f016_get_returns_503(monkeypatch: pytest.MonkeyPatch, api: TestClient) -> None:
    def fail_get(self, **kwargs: object) -> object:
        raise _operational_error()

    monkeypatch.setattr(ItemRepository, "get", fail_get)
    response = api.get(f"/v1/items/{uuid.uuid4()}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_f016_user_resolve_returns_503_without_creating_user(
    api_jwt: tuple[TestClient, MintToken],
    clean_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, mint_token = api_jwt

    def fail_resolve(self, issuer: str, subject: str) -> uuid.UUID:
        raise _operational_error()

    monkeypatch.setattr(UserRepository, "resolve", fail_resolve)
    with clean_db.connect() as connection:
        before = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    headers = {"Authorization": f"Bearer {mint_token({'sub': 'new-principal'})}"}
    response = client.post("/v1/items", json={"content_id": "x"}, headers=headers)
    with clean_db.connect() as connection:
        after = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert after == before
