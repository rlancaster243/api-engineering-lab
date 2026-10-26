"""Application settings (fail closed at startup)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(Exception):
    """Missing or invalid configuration."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str


def sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    database_url = environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required")
    return Settings(
        database_url=sqlalchemy_url(database_url),
        app_env=environ.get("APP_ENV", "").strip(),
    )
