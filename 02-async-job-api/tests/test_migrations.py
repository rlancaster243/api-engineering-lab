from sqlalchemy import Engine, text


def test_migration_upgrade_from_clean_database(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0001_initial_placeholder"
