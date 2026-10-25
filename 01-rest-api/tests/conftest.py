"""Shared fixtures.

Two kinds of test app:

- ``client``: a stub app mounting the real schemas and error handlers over canned data,
  so the contract can be tested without a database.
- ``api``: the real application against PostgreSQL. It needs ``TEST_DATABASE_URL``, a
  database the suite is allowed to wipe; it never reads ``DATABASE_URL``, so a test run
  cannot destroy a developer's working database. Without it, database tests skip, unless
  ``REQUIRE_DATABASE=1`` (set in CI), which turns the skip into a failure so a broken
  CI database cannot pass silently.
"""

import os
import time
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
import pytest
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from alembic import command
from app.config import OidcSettings, sqlalchemy_url
from app.errors import ApiError, ErrorCode, install_error_handling
from app.models import DEV_USER_ID
from app.schemas import (
    DEFAULT_LIMIT,
    ItemCreate,
    ItemPage,
    ItemPatch,
    ItemResponse,
    Limit,
)
from app.tokens import InvalidToken, TokenVerifier

_NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)


def build_contract_app() -> FastAPI:
    app = FastAPI()
    install_error_handling(app)

    @app.post("/v1/items", status_code=201, response_model=ItemResponse)
    def create_item(body: ItemCreate) -> ItemResponse:
        return ItemResponse(id=uuid.uuid4(), created_at=_NOW, updated_at=_NOW, **body.model_dump())

    @app.get("/v1/items", response_model=ItemPage)
    def list_items(
        response: Response, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
    ) -> ItemPage:
        response.headers["X-Applied-Limit"] = str(limit)
        return ItemPage(items=[], next_cursor=None)

    @app.get("/v1/items/{item_id}")
    def get_item(item_id: uuid.UUID) -> dict[str, str]:
        return {"id": str(item_id)}

    @app.patch("/v1/items/{item_id}")
    def patch_item(item_id: uuid.UUID, body: ItemPatch) -> JSONResponse:
        return JSONResponse(body.changes())

    @app.delete("/v1/items/{item_id}", status_code=204)
    def delete_item(item_id: uuid.UUID) -> None:
        return None

    @app.get("/_raise/{code}")
    def raise_api_error(code: ErrorCode) -> None:
        raise ApiError(code)

    @app.get("/_boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/_http/{status}")
    def raise_http(status: int) -> None:
        raise StarletteHTTPException(status_code=status, detail="secret internal detail")

    return app


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so an unhandled exception is rendered by the
    # installed handler, as a real server would, instead of re-raised into the test.
    return TestClient(build_contract_app(), raise_server_exceptions=False)


# --- PostgreSQL-backed fixtures ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_CURSOR_KEY = "test-only-cursor-signing-key-0123456789"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        if os.environ.get("REQUIRE_DATABASE") == "1":
            pytest.fail("REQUIRE_DATABASE=1 but TEST_DATABASE_URL is unset")
        pytest.skip("TEST_DATABASE_URL is unset; skipping PostgreSQL tests")
    return url


@pytest.fixture(scope="session")
def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session")
def engine(alembic_config: Config, database_url: str) -> Generator[Engine, None, None]:
    # Rebuild from nothing once per session: every run proves migrate-from-clean.
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    db_engine = create_engine(sqlalchemy_url(database_url))
    yield db_engine
    db_engine.dispose()


@pytest.fixture
def clean_db(engine: Engine) -> Engine:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM saved_items"))
        connection.execute(text("DELETE FROM users WHERE id <> :seed"), {"seed": DEV_USER_ID})
    return engine


@pytest.fixture
def api(
    clean_db: Engine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("AUTH_MODE", "local_dev")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CURSOR_SIGNING_KEY", TEST_CURSOR_KEY)
    # The context manager runs the lifespan, i.e. real startup and shutdown.
    with TestClient(app) as test_client:
        yield test_client


# --- Token fixtures (no network, no real identity provider) -----------------------------

OIDC_ISSUER = "https://issuer.test"
OIDC_AUDIENCE = "rest-api-lab"
OIDC_JWKS_URL = "https://issuer.test/.well-known/jwks.json"
RSA_KID = "rsa-key-1"
EC_KID = "ec-key-1"


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


class StaticKeySource:
    """In-memory ``KeySource``: kid to public key; an unknown kid is an invalid token."""

    def __init__(self, keys: dict[str, Any]) -> None:
        self.keys = keys

    def signing_key(self, kid: str) -> object:
        try:
            return self.keys[kid]
        except KeyError:
            raise InvalidToken("unknown signing key") from None


@pytest.fixture
def static_keys(rsa_key: rsa.RSAPrivateKey, ec_key: ec.EllipticCurvePrivateKey) -> StaticKeySource:
    return StaticKeySource({RSA_KID: rsa_key.public_key(), EC_KID: ec_key.public_key()})


@pytest.fixture
def oidc_settings() -> OidcSettings:
    return OidcSettings(
        issuer=OIDC_ISSUER,
        audience=OIDC_AUDIENCE,
        jwks_url=OIDC_JWKS_URL,
        algorithms=("RS256", "ES256"),
        leeway_seconds=30,
        jwks_cache_seconds=300,
        required_scope=None,
    )


MintToken = Callable[..., str]


@pytest.fixture
def mint_token(rsa_key: rsa.RSAPrivateKey, ec_key: ec.EllipticCurvePrivateKey) -> MintToken:
    """Build a signed token. A ``None`` value in ``claims`` or ``header`` removes the entry."""

    def mint(
        claims: dict[str, Any] | None = None,
        header: dict[str, Any] | None = None,
        *,
        key: Any = None,
        alg: str = "RS256",
        kid: str | None = None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": OIDC_ISSUER,
            "aud": OIDC_AUDIENCE,
            "sub": "user-123",
            "iat": now,
            "exp": now + 300,
        } | (claims or {})
        payload = {k: v for k, v in payload.items() if v is not None}
        if key is None:
            key = ec_key if alg.startswith("ES") else rsa_key
        headers = {"kid": kid or (EC_KID if alg.startswith("ES") else RSA_KID)} | (header or {})
        headers = {k: v for k, v in headers.items() if v is not None}
        # PyJWT leaves out "typ" when it is None; "JWT" is its default otherwise.
        if "typ" not in (header or {}):
            headers["typ"] = "JWT"
        return jwt.encode(payload, key, algorithm=alg, headers=headers)

    return mint


@pytest.fixture
def api_jwt(
    clean_db: Engine,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    static_keys: StaticKeySource,
    mint_token: MintToken,
) -> Generator[tuple[TestClient, MintToken], None, None]:
    """Real app in ``external_jwt`` with an in-memory key source (no network)."""
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("AUTH_MODE", "external_jwt")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CURSOR_SIGNING_KEY", TEST_CURSOR_KEY)
    monkeypatch.setenv("OIDC_ISSUER", OIDC_ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", OIDC_AUDIENCE)
    monkeypatch.setenv("OIDC_JWKS_URL", OIDC_JWKS_URL)
    with TestClient(app) as test_client:
        settings = test_client.app.state.settings
        test_client.app.state.token_verifier = TokenVerifier(settings.oidc, static_keys)
        yield test_client, mint_token
