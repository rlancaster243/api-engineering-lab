import os
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app import db
from app.main import app


def _database_url() -> str | None:
    value = os.environ.get("DATABASE_URL")
    if value is None or value.strip() == "":
        return None
    return value


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()
    if url is None:
        pytest.skip("DATABASE_URL is unset; skipping Postgres integration tests")
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Generator[str, None, None]:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    yield database_url


@pytest.fixture
def db_engine(migrated_database: str) -> Generator[Engine, None, None]:
    engine = create_engine(migrated_database, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE users CASCADE"))
        connection.execute(
            text(
                "INSERT INTO users (id, email) VALUES "
                "('00000000-0000-4000-8000-000000000001', 'dev@localhost')"
            )
        )
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_engine: Engine) -> Generator[TestClient, None, None]:
    db.reset_engine()
    with TestClient(app) as test_client:
        yield test_client
    db.reset_engine()
