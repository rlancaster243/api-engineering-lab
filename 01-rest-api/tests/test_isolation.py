"""Two-principal isolation over HTTP (spec PR-06, F-009 to F-011, F-027)."""

import concurrent.futures
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.models import DEV_USER_ID
from app.tokens import KeysUnavailable, TokenVerifier
from tests.conftest import OIDC_ISSUER, MintToken


def _auth(mint_token: MintToken, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_token({'sub': sub})}"}


def test_cross_user_get_patch_delete_are_not_found(
    api_jwt: tuple[TestClient, MintToken],
) -> None:
    client, mint_token = api_jwt
    sub_a, sub_b = "principal-a", "principal-b"
    created = client.post(
        "/v1/items",
        json={"content_id": "shared-namespace"},
        headers=_auth(mint_token, sub_a),
    )
    assert created.status_code == 201
    item_id = created.json()["id"]

    headers_b = _auth(mint_token, sub_b)
    assert client.get(f"/v1/items/{item_id}", headers=headers_b).status_code == 404
    patch = client.patch(
        f"/v1/items/{item_id}",
        json={"notes": "stolen"},
        headers=headers_b,
    )
    assert patch.status_code == 404
    assert client.delete(f"/v1/items/{item_id}", headers=headers_b).status_code == 404

    # Owner can still read; B did not mutate or delete.
    assert client.get(f"/v1/items/{item_id}", headers=_auth(mint_token, sub_a)).status_code == 200


def test_lists_are_disjoint_and_same_content_id_allowed(
    api_jwt: tuple[TestClient, MintToken],
) -> None:
    client, mint_token = api_jwt
    content_id = "both-can-save"
    assert (
        client.post(
            "/v1/items",
            json={"content_id": content_id},
            headers=_auth(mint_token, "list-a"),
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/items",
            json={"content_id": content_id},
            headers=_auth(mint_token, "list-b"),
        ).status_code
        == 201
    )

    list_a = client.get("/v1/items", headers=_auth(mint_token, "list-a")).json()["items"]
    list_b = client.get("/v1/items", headers=_auth(mint_token, "list-b")).json()["items"]
    assert len(list_a) == 1
    assert len(list_b) == 1
    assert list_a[0]["id"] != list_b[0]["id"]


def test_cursor_bound_to_owner(api_jwt: tuple[TestClient, MintToken]) -> None:
    client, mint_token = api_jwt
    for sub, content in (("cursor-a", "one"), ("cursor-a", "two")):
        assert (
            client.post(
                "/v1/items",
                json={"content_id": content},
                headers=_auth(mint_token, sub),
            ).status_code
            == 201
        )
    page = client.get("/v1/items", params={"limit": 1}, headers=_auth(mint_token, "cursor-a"))
    assert page.status_code == 200
    cursor = page.json()["next_cursor"]
    assert cursor is not None
    stolen = client.get(
        "/v1/items",
        params={"limit": 1, "cursor": cursor},
        headers=_auth(mint_token, "cursor-b"),
    )
    assert stolen.status_code == 400
    assert stolen.json()["error"]["code"] == "INVALID_CURSOR"


def test_seed_user_never_resolved_in_external_jwt(
    api_jwt: tuple[TestClient, MintToken], clean_db: Engine
) -> None:
    client, mint_token = api_jwt
    assert (
        client.post(
            "/v1/items",
            json={"content_id": "external-only"},
            headers=_auth(mint_token, "not-the-seed"),
        ).status_code
        == 201
    )
    with clean_db.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
        seed_items = connection.execute(
            text("SELECT count(*) FROM saved_items WHERE user_id = :id"),
            {"id": DEV_USER_ID},
        ).scalar_one()
    assert count == 2  # seed row plus one external principal
    assert seed_items == 0


def test_concurrent_resolve_creates_one_user_row(clean_db: Engine) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.users import UserRepository

    subject = "concurrent-subject"
    issuer_b = "https://issuer-b.test"

    def resolve_once(issuer: str) -> uuid.UUID:
        session = sessionmaker(bind=clean_db, autoflush=False, autocommit=False)()
        try:
            return UserRepository(session).resolve(issuer, subject)
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: resolve_once(OIDC_ISSUER), range(8)))
    assert len(set(ids)) == 1

    other_id = resolve_once(issuer_b)
    assert other_id != ids[0]
    with clean_db.connect() as connection:
        rows = connection.execute(
            text("SELECT count(*) FROM users WHERE subject = :sub"),
            {"sub": subject},
        ).scalar_one()
    assert rows == 2


def test_keys_unavailable_does_not_write_users(
    api_jwt: tuple[TestClient, MintToken], clean_db: Engine, mint_token: MintToken
) -> None:
    client, _ = api_jwt

    class BrokenKeys:
        def signing_key(self, kid: str) -> object:
            raise KeysUnavailable("down")

    settings = client.app.state.settings
    client.app.state.token_verifier = TokenVerifier(settings.oidc, BrokenKeys())
    with clean_db.connect() as connection:
        before = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    response = client.get("/v1/items", headers=_auth(mint_token, "new-user"))
    with clean_db.connect() as connection:
        after = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert after == before
