"""Process configuration, read once at startup.

The service fails closed in production: a deployment that has not said where its
database lives, or which environment it is in, stops at startup instead of writing to
a path relative to the process working directory or seeding the documentation tenants.

``APP_ENV`` has no default anywhere. The tenants whose keys are published in the README
exist only when an operator has said, in so many words, that this is ``local`` or
``test``. An unset or unrecognised value is a startup failure, not a silent mode change.
"""

import os
from dataclasses import dataclass

DEFAULT_DATABASE_PATH = "var/assets.sqlite3"

#: Environments that seed the two documentation tenants in the README.
LOCAL_ENVS = ("local", "test")

#: Every value ``APP_ENV`` may take. Anything else refuses to start.
KNOWN_ENVS = (*LOCAL_ENVS, "production")


@dataclass(frozen=True)
class Settings:
    database_path: str
    # No default: a default here would be a second place deciding local mode.
    app_env: str


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "").strip()
    if not app_env:
        raise RuntimeError(f"APP_ENV is required; set it to one of {', '.join(KNOWN_ENVS)}")
    if app_env not in KNOWN_ENVS:
        raise RuntimeError(
            f"APP_ENV={app_env!r} is not recognised; it must be one of {', '.join(KNOWN_ENVS)}"
        )
    configured_path = os.environ.get("DATABASE_PATH", "").strip()
    if not configured_path:
        if app_env not in LOCAL_ENVS:
            raise RuntimeError(
                f"DATABASE_PATH is required when APP_ENV={app_env!r}; "
                f"it has a default only in {', '.join(LOCAL_ENVS)}"
            )
        configured_path = DEFAULT_DATABASE_PATH
    return Settings(database_path=configured_path, app_env=app_env)
