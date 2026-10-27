"""Migration tests."""

import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import sqlalchemy_url

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_migration_round_trip(migrated_database_url: str) -> None:
    """Downgrade to base then upgrade to head on an isolated scratch database.

    Uses its own CREATE/DROP DATABASE so it is independent of test ordering and
    of the session-scoped migrated_engine fixture.
    """
    parsed = urlparse(migrated_database_url)
    base_name = parsed.path.lstrip("/")
    suffix = "_roundtrip"
    scratch_name = base_name[: 63 - len(suffix)] + suffix
    maintenance_url = urlunparse(parsed._replace(path="/postgres"))
    scratch_url = urlunparse(parsed._replace(path=f"/{scratch_name}"))

    maint = psycopg.connect(maintenance_url, autocommit=True)
    try:
        maint.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        maint.close()

    scratch_cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    scratch_cfg.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    scratch_cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(scratch_url))

    engine = create_engine(sqlalchemy_url(scratch_url))
    try:
        # Full cycle: upgrade → downgrade → upgrade so every UPGRADE and DOWNGRADE
        # path is exercised. Skipping the first upgrade means downgrade is a no-op
        # and the test misses DROP TABLE / DROP FUNCTION failures.
        command.upgrade(scratch_cfg, "head")
        command.downgrade(scratch_cfg, "base")
        command.upgrade(scratch_cfg, "head")

        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == "0005_job_effects"
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'jobs'"
                )
            ).scalar_one_or_none()
            assert result == "jobs"
    finally:
        engine.dispose()
        drop = psycopg.connect(maintenance_url, autocommit=True)
        try:
            drop.execute(f'DROP DATABASE "{scratch_name}" WITH (FORCE)')
        finally:
            drop.close()


def test_migration_upgrade_from_clean_database(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0005_job_effects"


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


def test_trg_jobs_updated_at_fires(migrated_engine: Engine) -> None:
    """trg_jobs_updated_at must advance updated_at on every UPDATE.

    INSERT and UPDATE run in separate transactions so PostgreSQL's transaction-scoped
    now() returns a later timestamp for the UPDATE. A 50 ms sleep between them
    ensures the difference is measurable even on fast machines.

    The UPDATE deliberately leaves status alone: since 0003_job_leases a transition
    to RUNNING requires a lease (ck_jobs_running_has_lease), and this test is about
    the trigger, not the lease protocol.
    """
    job_id = str(uuid.uuid4())

    with migrated_engine.connect() as conn:
        conn.execute(
            text("INSERT INTO jobs (id, type, payload) VALUES (:id, 'trigger.test', '{}'::jsonb)"),
            {"id": job_id},
        )
        conn.commit()

    time.sleep(0.05)

    with migrated_engine.connect() as conn:
        conn.execute(
            text("UPDATE jobs SET payload = '{\"touched\": true}'::jsonb WHERE id = :id"),
            {"id": job_id},
        )
        row = conn.execute(
            text("SELECT created_at, updated_at FROM jobs WHERE id = :id"), {"id": job_id}
        ).one()
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        conn.commit()

    assert row.updated_at > row.created_at, (
        f"updated_at ({row.updated_at}) must be after created_at ({row.created_at}) "
        "after an UPDATE — trigger may not be firing"
    )


def test_ck_jobs_updated_after_created_rejects_violation(migrated_engine: Engine) -> None:
    """ck_jobs_updated_after_created must reject updated_at earlier than created_at."""
    job_id = str(uuid.uuid4())
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, updated_at) "
                    "VALUES (:id, 'constraint.test', '{}'::jsonb, '2000-01-01'::timestamptz)"
                ),
                {"id": job_id},
            )
            conn.commit()


def test_0003_adds_lease_columns(migrated_engine: Engine) -> None:
    """The four lease columns from 0003_job_leases exist and are nullable."""
    with migrated_engine.connect() as connection:
        columns = dict(
            connection.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'jobs'"
                )
            ).all()
        )
    for column in ("lease_owner", "lease_expires_at", "claimed_at", "finished_at"):
        assert column in columns, f"0003_job_leases did not add {column}"
        assert columns[column] == "YES", f"{column} must be nullable"


def test_0003_adds_lease_index(migrated_engine: Engine) -> None:
    """ix_jobs_lease_expiry must exist as a partial index on RUNNING rows."""
    with migrated_engine.connect() as connection:
        definition = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_jobs_lease_expiry'")
        ).scalar_one_or_none()
    assert definition is not None, "ix_jobs_lease_expiry is missing"
    assert "lease_expires_at" in definition
    assert "RUNNING" in definition, "the lease index must be partial on RUNNING rows"


def test_lease_columns_downgrade_cleanly(migrated_database_url: str) -> None:
    """Downgrading 0003 then re-upgrading must leave the schema usable.

    Exercises the DOWNGRADE path in isolation; test_migration_round_trip covers the
    whole chain, but a broken single step is easier to attribute here.
    """
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", sqlalchemy_url(migrated_database_url))

    engine = create_engine(sqlalchemy_url(migrated_database_url))
    try:
        command.downgrade(config, "0002_jobs")
        with engine.connect() as conn:
            remaining = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'jobs' "
                    "AND column_name IN "
                    "('lease_owner','lease_expires_at','claimed_at','finished_at')"
                )
            ).scalar_one()
        assert remaining == 0, "downgrade left lease columns behind"
    finally:
        command.upgrade(config, "head")
        engine.dispose()

    with create_engine(sqlalchemy_url(migrated_database_url)).connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0005_job_effects"


def test_0004_adds_next_attempt_at(migrated_engine: Engine) -> None:
    """next_attempt_at must exist, be NOT NULL, and default to now()."""
    with migrated_engine.connect() as connection:
        column = connection.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'jobs' "
                "AND column_name = 'next_attempt_at'"
            )
        ).one_or_none()
    assert column is not None, "0004_job_retry did not add next_attempt_at"
    assert column.is_nullable == "NO"
    assert column.column_default is not None and "now()" in column.column_default


def test_0004_backfill_matches_created_at(migrated_database_url: str) -> None:
    """Rows present before the migration must be backfilled to their created_at.

    Exercises the real upgrade path on a scratch database: downgrade to 0003,
    insert a row, then upgrade so the UPDATE ... SET next_attempt_at = created_at
    actually runs against existing data.
    """
    parsed = urlparse(migrated_database_url)
    base_name = parsed.path.lstrip("/")
    suffix = "_backfill"
    scratch_name = base_name[: 63 - len(suffix)] + suffix
    maintenance_url = urlunparse(parsed._replace(path="/postgres"))
    scratch_url = urlunparse(parsed._replace(path=f"/{scratch_name}"))

    maint = psycopg.connect(maintenance_url, autocommit=True)
    try:
        maint.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        maint.close()

    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", sqlalchemy_url(scratch_url))

    engine = create_engine(sqlalchemy_url(scratch_url))
    try:
        command.upgrade(config, "0003_job_leases")
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, created_at, updated_at) "
                    "VALUES (:id, 'echo', '{}'::jsonb, now() - interval '1 hour', now())"
                ),
                {"id": str(uuid.uuid4())},
            )
            conn.commit()

        command.upgrade(config, "head")

        with engine.connect() as conn:
            backfilled = conn.execute(
                text(
                    "SELECT next_attempt_at = created_at AS matches, "
                    "next_attempt_at IS NOT NULL AS present FROM jobs"
                )
            ).one()
        assert backfilled.present, "existing row was not backfilled"
        assert backfilled.matches, "backfill must equal created_at for existing rows"
    finally:
        engine.dispose()
        drop = psycopg.connect(maintenance_url, autocommit=True)
        try:
            drop.execute(f'DROP DATABASE "{scratch_name}" WITH (FORCE)')
        finally:
            drop.close()


def test_0005_creates_job_effects_with_partial_index(migrated_engine: Engine) -> None:
    """The outbox table, its composite key and its pending-only index must exist."""
    with migrated_engine.connect() as connection:
        columns = dict(
            connection.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'job_effects'"
                )
            ).all()
        )
    for column in ("job_id", "effect_key", "status", "detail", "created_at", "applied_at"):
        assert column in columns, f"0005_job_effects did not create {column}"
    assert columns["status"] == "NO"
    assert columns["created_at"] == "NO"
    assert columns["detail"] == "YES"
    assert columns["applied_at"] == "YES"

    with migrated_engine.connect() as connection:
        definition = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_job_effects_pending'")
        ).scalar_one_or_none()
        key = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'job_effects_pkey'"
            )
        ).scalar_one_or_none()
    assert definition is not None, "ix_job_effects_pending is missing"
    assert "created_at" in definition
    assert "INTENT" in definition, "the pending index must be partial on INTENT rows"
    assert key is not None and "(job_id, effect_key)" in key, (
        "the outbox key must be (job_id, effect_key)"
    )
