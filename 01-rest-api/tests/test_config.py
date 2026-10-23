"""Startup configuration fails closed (spec section 5.3, ADR 0007). No database needed."""

import pytest
from fastapi.testclient import TestClient

from app.config import AuthMode, load_settings
from app.pagination import ConfigError

KEY = "k" * 32
BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost:5432/db",
    "CURSOR_SIGNING_KEY": KEY,
}


def test_default_mode_is_external_jwt() -> None:
    assert load_settings(BASE).auth_mode is AuthMode.EXTERNAL_JWT


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_local_dev_is_allowed_only_in_local_and_test(app_env: str) -> None:
    settings = load_settings(BASE | {"AUTH_MODE": "local_dev", "APP_ENV": app_env})
    assert settings.auth_mode is AuthMode.LOCAL_DEV


@pytest.mark.parametrize("app_env", ["", "production", "staging", "ci", "LOCAL"])
def test_f025_local_dev_outside_local_or_test_refuses_to_start(app_env: str) -> None:
    with pytest.raises(ConfigError, match="local_dev"):
        load_settings(BASE | {"AUTH_MODE": "local_dev", "APP_ENV": app_env})


def test_unknown_auth_mode_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="AUTH_MODE"):
        load_settings(BASE | {"AUTH_MODE": "none"})


@pytest.mark.parametrize("missing", ["DATABASE_URL", "CURSOR_SIGNING_KEY"])
def test_missing_required_setting_refuses_to_start(missing: str) -> None:
    environ = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_settings(environ)


def test_short_cursor_key_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="CURSOR_SIGNING_KEY"):
        load_settings(BASE | {"CURSOR_SIGNING_KEY": "short"})


def test_plain_postgresql_url_is_given_the_psycopg_driver() -> None:
    assert load_settings(BASE).database_url == "postgresql+psycopg://u:p@localhost:5432/db"


def test_application_startup_fails_without_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    for name in ("DATABASE_URL", "CURSOR_SIGNING_KEY", "AUTH_MODE", "APP_ENV"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError), TestClient(app):
        pass


def test_f026_external_jwt_mode_never_resolves_the_seed_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    # No verifier exists until PR-05, so every item route must refuse. The database URL
    # is never connected to: the refusal happens before any query.
    for name, value in BASE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AUTH_MODE", "external_jwt")
    monkeypatch.delenv("APP_ENV", raising=False)
    with TestClient(app) as client:
        for response in (
            client.get("/v1/items"),
            client.post("/v1/items", json={"content_id": "x"}),
            client.get("/v1/items/00000000-0000-0000-0000-000000000001"),
        ):
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "UNAUTHENTICATED"
            assert response.headers["WWW-Authenticate"] == "Bearer"
