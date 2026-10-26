import os
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from alembic import command
from app.config import sqlalchemy_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/async_job_api_lab"


@pytest.fixture
def database_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        if os.environ.get("REQUIRE_DATABASE") == "1":
            pytest.fail("REQUIRE_DATABASE=1 but TEST_DATABASE_URL is unset")
        url = DEFAULT_DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture
def client(database_url: str) -> Generator[TestClient, None, None]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        if os.environ.get("REQUIRE_DATABASE") == "1":
            pytest.fail("REQUIRE_DATABASE=1 but TEST_DATABASE_URL is unset")
        pytest.skip("TEST_DATABASE_URL is unset; skipping PostgreSQL tests")
    return url


@pytest.fixture(scope="session")
def alembic_config(migrated_database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", migrated_database_url)
    return config


@pytest.fixture(scope="session")
def migrated_engine(
    alembic_config: Config, migrated_database_url: str
) -> Generator[Engine, None, None]:
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    engine = create_engine(sqlalchemy_url(migrated_database_url))
    yield engine
    engine.dispose()
