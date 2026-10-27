import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError


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
