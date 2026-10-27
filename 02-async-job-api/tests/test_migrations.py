"""Migration tests.

test_migration_round_trip runs first and is self-contained (own engine, explicit
downgrade→upgrade). It leaves the schema at head so the session-scoped
migrated_engine fixture (which also does downgrade→upgrade on its own) works
correctly for every subsequent test.
"""

import uuid

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import sqlalchemy_url


def test_migration_round_trip(alembic_config: Config, migrated_database_url: str) -> None:
    """Downgrade to base then upgrade to head proves the round-trip is clean.

    Leaves the schema at head so later tests that depend on migrated_engine
    find the correct state.
    """
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_engine(sqlalchemy_url(migrated_database_url))
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == "0002_jobs"
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'jobs'"
                )
            ).scalar_one_or_none()
            assert result == "jobs"
    finally:
        engine.dispose()


def test_migration_upgrade_from_clean_database(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0002_jobs"


def test_jobs_table_exists(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        result = connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'jobs'"
            )
        ).scalar_one_or_none()
    assert result == "jobs"


def test_ck_jobs_status_rejects_invalid_value(migrated_engine: Engine) -> None:
    """ck_jobs_status must reject values outside the allowed set."""
    job_id = str(uuid.uuid4())
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, status) "
                    "VALUES (:id, 'test.job', '{}', 'INVALID_STATUS')"
                ),
                {"id": job_id},
            )
            conn.commit()
