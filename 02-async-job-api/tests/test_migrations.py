from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from app.config import sqlalchemy_url


def test_migration_upgrade_from_clean_database(
    alembic_config: Config, migrated_database_url: str
) -> None:
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    engine = create_engine(sqlalchemy_url(migrated_database_url))
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    engine.dispose()
    assert version == "0001_initial_placeholder"
