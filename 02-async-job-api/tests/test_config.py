import pytest

from app.config import ConfigError, load_settings


def test_missing_database_url_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings({})


def test_load_settings_accepts_plain_postgresql_url() -> None:
    settings = load_settings(
        {"DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/async_job_api_lab"}
    )
    assert settings.database_url.startswith("postgresql+psycopg://")
