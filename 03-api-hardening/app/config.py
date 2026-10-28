"""Process configuration, read once at startup.

The service fails closed in production: a deployment that has not said where its
database lives stops at startup instead of writing to a path relative to the process
working directory.
"""

import os
from dataclasses import dataclass

DEFAULT_DATABASE_PATH = "var/assets.sqlite3"
LOCAL_ENVS = ("local", "test")


@dataclass(frozen=True)
class Settings:
    database_path: str
    app_env: str = "local"


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "local").strip() or "local"
    configured_path = os.environ.get("DATABASE_PATH", "").strip()
    if not configured_path:
        if app_env not in LOCAL_ENVS:
            raise RuntimeError(
                f"DATABASE_PATH is required when APP_ENV={app_env!r}; "
                f"it has a default only in {', '.join(LOCAL_ENVS)}"
            )
        configured_path = DEFAULT_DATABASE_PATH
    return Settings(database_path=configured_path, app_env=app_env)
