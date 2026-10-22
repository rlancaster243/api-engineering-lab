"""Trusted settings, read once at startup (spec section 5.3, ADR 0007, ADR 0008).

Every rule here fails closed: a missing or unsafe value stops the application from
starting instead of falling back to a default that could weaken it.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.pagination import ConfigError, load_cursor_key


class AuthMode(StrEnum):
    EXTERNAL_JWT = "external_jwt"
    LOCAL_DEV = "local_dev"


# The fixed development owner may only be used where nothing is publicly reachable.
LOCAL_DEV_ENVS = frozenset({"local", "test"})


@dataclass(frozen=True)
class Settings:
    database_url: str
    auth_mode: AuthMode
    app_env: str
    cursor_key: bytes


def sqlalchemy_url(url: str) -> str:
    """Accept the plain ``postgresql://`` form that psql also understands.

    One ``DATABASE_URL`` then works for the app, Alembic and psql; only the
    SQLAlchemy side needs the explicit psycopg 3 driver.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


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
    )
