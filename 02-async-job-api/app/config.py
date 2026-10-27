"""Application settings (fail closed at startup)."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(Exception):
    """Missing or invalid configuration."""


DEFAULT_WORKER_POLL_SECONDS = 1.0
DEFAULT_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 3600
DEFAULT_RETRY_BACKOFF_SECONDS = 5
DEFAULT_MAX_RETRY_BACKOFF_SECONDS = 300
_MAX_BACKOFF_SECONDS = 3600
_MAX_WORKER_POLL_SECONDS = 3600.0


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str
    worker_poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS
    max_retry_backoff_seconds: int = DEFAULT_MAX_RETRY_BACKOFF_SECONDS


def sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _positive_float(environ: Mapping[str, str], name: str, default: float, maximum: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number, got {raw!r}")
    if not 0 < value <= maximum:
        raise ConfigError(f"{name} must be greater than 0 and at most {maximum:g}, got {raw!r}")
    return value


def _bounded_int(
    environ: Mapping[str, str], name: str, default: int, *, maximum: int, inclusive: bool
) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    within = value <= maximum if inclusive else value < maximum
    if not (value > 0 and within):
        bound = "less than or equal to" if inclusive else "less than"
        raise ConfigError(f"{name} must be greater than 0 and {bound} {maximum}, got {raw!r}")
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


def _retry_backoff(environ: Mapping[str, str]) -> tuple[int, int]:
    backoff = _bounded_int(
        environ,
        "RETRY_BACKOFF_SECONDS",
        DEFAULT_RETRY_BACKOFF_SECONDS,
        maximum=_MAX_BACKOFF_SECONDS,
        inclusive=True,
    )
    ceiling = _bounded_int(
        environ,
        "MAX_RETRY_BACKOFF_SECONDS",
        DEFAULT_MAX_RETRY_BACKOFF_SECONDS,
        maximum=_MAX_BACKOFF_SECONDS,
        inclusive=True,
    )
    if ceiling < backoff:
        raise ConfigError(
            "MAX_RETRY_BACKOFF_SECONDS must be greater than or equal to "
            f"RETRY_BACKOFF_SECONDS ({backoff}), got {ceiling}"
        )
    return backoff, ceiling


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    database_url = environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required")
    retry_backoff, max_retry_backoff = _retry_backoff(environ)
    return Settings(
        database_url=sqlalchemy_url(database_url),
        app_env=environ.get("APP_ENV", "").strip(),
        worker_poll_seconds=_positive_float(
            environ,
            "WORKER_POLL_SECONDS",
            DEFAULT_WORKER_POLL_SECONDS,
            _MAX_WORKER_POLL_SECONDS,
        ),
        lease_seconds=_lease_seconds(environ),
        retry_backoff_seconds=retry_backoff,
        max_retry_backoff_seconds=max_retry_backoff,
    )


def retry_delay_seconds(attempt: int, *, base: int, ceiling: int) -> int:
    """Exponential backoff after a failed attempt, capped at ``ceiling``."""
    if attempt < 1:
        return base
    return min(base * 2 ** (attempt - 1), ceiling)
