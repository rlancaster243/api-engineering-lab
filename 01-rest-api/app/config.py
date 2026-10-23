"""Trusted settings, read once at startup (spec section 5.3, ADR 0007, ADR 0008).

Every rule here fails closed: a missing or unsafe value stops the application from
starting instead of falling back to a default that could weaken it.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from app.pagination import ConfigError, load_cursor_key


class AuthMode(StrEnum):
    EXTERNAL_JWT = "external_jwt"
    LOCAL_DEV = "local_dev"


# The fixed development owner may only be used where nothing is publicly reachable.
LOCAL_DEV_ENVS = frozenset({"local", "test"})


# Asymmetric algorithms only. HS* is excluded so a public key can never be used as an HMAC
# secret, and "none" so an unsigned token can never verify (ADR 0005).
ALLOWED_ALGORITHMS = frozenset({"RS256", "PS256", "ES256"})


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    audience: str
    jwks_url: str
    algorithms: tuple[str, ...]
    leeway_seconds: int
    jwks_cache_seconds: int
    required_scope: str | None


@dataclass(frozen=True)
class Settings:
    database_url: str
    auth_mode: AuthMode
    app_env: str
    cursor_key: bytes
    oidc: OidcSettings | None


def sqlalchemy_url(url: str) -> str:
    """Accept the plain ``postgresql://`` form that psql also understands.

    One ``DATABASE_URL`` then works for the app, Alembic and psql; only the
    SQLAlchemy side needs the explicit psycopg 3 driver.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required when AUTH_MODE=external_jwt")
    return value


def _bounded_int(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def load_oidc_settings(environ: Mapping[str, str]) -> OidcSettings:
    """Read the token-verification settings; every problem names its variable."""
    issuer = _required(environ, "OIDC_ISSUER")
    audience = _required(environ, "OIDC_AUDIENCE")

    jwks_url = _required(environ, "OIDC_JWKS_URL")
    # Keys fetched over plain HTTP could be swapped in transit, which would let anyone
    # mint tokens the service accepts.
    parts = urlsplit(jwks_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError("OIDC_JWKS_URL must be an https:// URL with a host")

    algorithms = tuple(
        a.strip() for a in environ.get("OIDC_ALGORITHMS", "RS256").split(",") if a.strip()
    )
    if not algorithms:
        raise ConfigError("OIDC_ALGORITHMS must list at least one algorithm")
    unsupported = [a for a in algorithms if a not in ALLOWED_ALGORITHMS]
    if unsupported:
        raise ConfigError(
            f"OIDC_ALGORITHMS may only contain {sorted(ALLOWED_ALGORITHMS)}, got {unsupported}"
        )

    return OidcSettings(
        issuer=issuer,
        audience=audience,
        jwks_url=jwks_url,
        algorithms=algorithms,
        leeway_seconds=_bounded_int(environ, "OIDC_LEEWAY_SECONDS", 30, 0, 120),
        jwks_cache_seconds=_bounded_int(environ, "OIDC_JWKS_CACHE_SECONDS", 300, 1, 86400),
        required_scope=environ.get("OIDC_REQUIRED_SCOPE", "").strip() or None,
    )


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    database_url = environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required")

    raw_mode = environ.get("AUTH_MODE", AuthMode.EXTERNAL_JWT.value)
    try:
        auth_mode = AuthMode(raw_mode)
    except ValueError:
        raise ConfigError(f"AUTH_MODE must be one of {[m.value for m in AuthMode]}") from None

    app_env = environ.get("APP_ENV", "")
    if auth_mode is AuthMode.LOCAL_DEV and app_env not in LOCAL_DEV_ENVS:
        raise ConfigError(
            f"AUTH_MODE=local_dev requires APP_ENV in {sorted(LOCAL_DEV_ENVS)}, got {app_env!r}"
        )

    return Settings(
        database_url=sqlalchemy_url(database_url),
        auth_mode=auth_mode,
        app_env=app_env,
        cursor_key=load_cursor_key(environ),
        oidc=load_oidc_settings(environ) if auth_mode is AuthMode.EXTERNAL_JWT else None,
    )
