"""Application settings (fail closed at startup)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(Exception):
    """Missing or invalid configuration."""


DEFAULT_WORKER_POLL_SECONDS = 1.0
DEFAULT_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 3600


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str
    worker_poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS


def sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not value > 0:
        raise ConfigError(f"{name} must be greater than 0, got {raw!r}")
    return value


def _lease_seconds(environ: Mapping[str, str]) -> int:
    raw = environ.get("LEASE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_LEASE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"LEASE_SECONDS must be an integer, got {raw!r}") from None
    if not 0 < value < _MAX_LEASE_SECONDS:
        raise ConfigError(
            f"LEASE_SECONDS must be greater than 0 and less than {_MAX_LEASE_SECONDS}, got {raw!r}"
        )
    return value


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    database_url = environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required")
    return Settings(
        database_url=sqlalchemy_url(database_url),
        app_env=environ.get("APP_ENV", "").strip(),
        worker_poll_seconds=_positive_float(
            environ, "WORKER_POLL_SECONDS", DEFAULT_WORKER_POLL_SECONDS
        ),
        lease_seconds=_lease_seconds(environ),
    )
