"""Startup configuration fails closed (spec section 5.3, ADR 0007). No database needed."""

import pytest
from fastapi.testclient import TestClient

from app.config import AuthMode, load_settings
from app.pagination import ConfigError
from app.tokens import TokenVerifier
from tests.conftest import (
    OIDC_AUDIENCE,
    OIDC_ISSUER,
    OIDC_JWKS_URL,
    MintToken,
    StaticKeySource,
)

KEY = "k" * 32
BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost:5432/db",
    "CURSOR_SIGNING_KEY": KEY,
    "OIDC_ISSUER": OIDC_ISSUER,
    "OIDC_AUDIENCE": OIDC_AUDIENCE,
    "OIDC_JWKS_URL": OIDC_JWKS_URL,
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


@pytest.mark.parametrize("missing", ["OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL"])
def test_f022_missing_oidc_setting_refuses_to_start_in_external_jwt(missing: str) -> None:
    environ = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_settings(environ)


@pytest.mark.parametrize("blank", ["OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL"])
def test_f022_blank_oidc_setting_refuses_to_start(blank: str) -> None:
    with pytest.raises(ConfigError, match=blank):
        load_settings(BASE | {blank: "  "})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OIDC_JWKS_URL", "http://issuer.test/jwks.json"),
        ("OIDC_JWKS_URL", "https:///jwks.json"),
        ("OIDC_JWKS_URL", "issuer.test/jwks.json"),
        ("OIDC_ALGORITHMS", "HS256"),
        ("OIDC_ALGORITHMS", "none"),
        ("OIDC_ALGORITHMS", "RS256,HS256"),
        ("OIDC_ALGORITHMS", " , "),
        ("OIDC_LEEWAY_SECONDS", "121"),
        ("OIDC_LEEWAY_SECONDS", "-1"),
        ("OIDC_LEEWAY_SECONDS", "soon"),
        ("OIDC_JWKS_CACHE_SECONDS", "0"),
        ("OIDC_JWKS_CACHE_SECONDS", "86401"),
    ],
)
def test_f022_unsafe_oidc_setting_refuses_to_start(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(BASE | {name: value})


def test_oidc_defaults_and_overrides() -> None:
    oidc = load_settings(BASE).oidc
    assert oidc is not None
    assert (oidc.algorithms, oidc.leeway_seconds, oidc.jwks_cache_seconds) == (("RS256",), 30, 300)
    assert oidc.required_scope is None

    tuned = load_settings(
        BASE
        | {
            "OIDC_ALGORITHMS": "RS256, ES256",
            "OIDC_LEEWAY_SECONDS": "0",
            "OIDC_JWKS_CACHE_SECONDS": "86400",
            "OIDC_REQUIRED_SCOPE": "items:write",
        }
    ).oidc
    assert tuned is not None
    assert tuned.algorithms == ("RS256", "ES256")
    assert (tuned.leeway_seconds, tuned.jwks_cache_seconds) == (0, 86400)
    assert tuned.required_scope == "items:write"


def test_blank_required_scope_means_none() -> None:
    oidc = load_settings(BASE | {"OIDC_REQUIRED_SCOPE": "  "}).oidc
    assert oidc is not None and oidc.required_scope is None


def test_local_dev_does_not_require_or_parse_oidc_settings() -> None:
    environ = {
        "DATABASE_URL": BASE["DATABASE_URL"],
        "CURSOR_SIGNING_KEY": KEY,
        "AUTH_MODE": "local_dev",
        "APP_ENV": "local",
        "OIDC_ALGORITHMS": "HS256",  # would be fatal in external_jwt; ignored here
    }
    assert load_settings(environ).oidc is None


def _start_external_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in BASE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AUTH_MODE", "external_jwt")
    monkeypatch.delenv("APP_ENV", raising=False)


def test_startup_builds_a_verifier_in_external_jwt_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    _start_external_jwt(monkeypatch)
    # Any network attempt would raise here: startup must only construct the key source.
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail("network call at startup"),
    )
    with TestClient(app):
        assert isinstance(app.state.token_verifier, TokenVerifier)


def test_startup_has_no_verifier_in_local_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    for name, value in BASE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AUTH_MODE", "local_dev")
    monkeypatch.setenv("APP_ENV", "local")
    with TestClient(app):
        assert app.state.token_verifier is None


ITEM_REQUESTS = (
    ("GET", "/v1/items", None),
    ("POST", "/v1/items", {"content_id": "x"}),
    ("GET", "/v1/items/00000000-0000-0000-0000-000000000001", None),
)


def test_f026_external_jwt_mode_never_resolves_the_seed_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    # Without a credential every item route must refuse. The database URL is never
    # connected to: the refusal happens before any query.
    _start_external_jwt(monkeypatch)
    with TestClient(app) as client:
        for method, path, body in ITEM_REQUESTS:
            response = client.request(method, path, json=body)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "UNAUTHENTICATED"
            assert response.headers["WWW-Authenticate"] == "Bearer"


def test_f026_a_valid_token_still_gets_401_until_pr06(
    monkeypatch: pytest.MonkeyPatch, static_keys: StaticKeySource, mint_token: MintToken
) -> None:
    from app.main import app

    # PR-05 staging rule: verification exists, but nothing maps a principal to a user
    # yet, so item routes stay closed even for a genuine token. PR-06 changes this test.
    _start_external_jwt(monkeypatch)
    with TestClient(app) as client:
        settings = app.state.settings
        app.state.token_verifier = TokenVerifier(settings.oidc, static_keys)
        headers = {"Authorization": f"Bearer {mint_token()}"}
        for method, path, body in ITEM_REQUESTS:
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "UNAUTHENTICATED"
