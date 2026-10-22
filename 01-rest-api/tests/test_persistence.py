"""Database-level invariants, below the HTTP layer. IDs refer to spec section 8."""

import threading
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.models import DEV_USER_ID
from app.repository import DuplicateItem, ItemRepository


def _create(engine: Engine, **overrides: object):
    values = {
        "user_id": DEV_USER_ID,
        "item_id": uuid.uuid4(),
        "content_id": "c",
        "title": None,
        "url": None,
        "notes": None,
    } | overrides
    session = sessionmaker(bind=engine)()
    try:
        return ItemRepository(session).create(**values)
    finally:
        session.close()


def _count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM saved_items")).scalar_one()


def test_f004_concurrent_duplicate_saves_create_exactly_one_row(clean_db: Engine) -> None:
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def save() -> None:
        barrier.wait()
        try:
            _create(clean_db, content_id="race")
            result = "created"
        except DuplicateItem:
            result = "duplicate"
        except Exception as exc:  # any other outcome is a failure
            result = f"unexpected: {exc!r}"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=save) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["created"] + ["duplicate"] * (workers - 1)
    assert _count(clean_db) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_id": uuid.uuid4()},  # FK violation (23503)
        {"content_id": "\t"},  # CHECK violation (23514): bypasses API normalization
    ],
    ids=["foreign-key", "check"],
)
def test_f005_f028_other_integrity_errors_are_not_reported_as_duplicates(
    clean_db: Engine, overrides: dict
) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        _create(clean_db, **overrides)
    assert not isinstance(excinfo.value, DuplicateItem)
    assert _count(clean_db) == 0


def test_f028_primary_key_collision_is_not_a_duplicate_save(clean_db: Engine) -> None:
    # Same SQLSTATE (23505) as a duplicate save, different constraint.
    item_id = uuid.uuid4()
    _create(clean_db, item_id=item_id, content_id="first")
    with pytest.raises(IntegrityError) as excinfo:
        _create(clean_db, item_id=item_id, content_id="second")
    assert excinfo.value.orig.diag.constraint_name == "saved_items_pkey"
    assert _count(clean_db) == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("content_id", "\t"),
        ("content_id", "\x0b"),
        ("content_id", " padded"),
        ("content_id", "padded\n"),
        ("title", "   "),
        ("url", "javascript:alert(1)"),
        ("url", "https://" + "a" * 2050),
        ("notes", "n" * 2001),
    ],
)
def test_f033_database_checks_reject_what_the_api_rejects(
    clean_db: Engine, column: str, value: str
) -> None:
    # Direct SQL, as a seed script or a future worker would write, bypassing the API.
    row = {"id": uuid.uuid4(), "user_id": DEV_USER_ID, "content_id": "ok", column: value}
    columns = ", ".join(row)
    params = ", ".join(f":{name}" for name in row)
    with pytest.raises(IntegrityError) as excinfo, clean_db.begin() as connection:
        connection.execute(text(f"INSERT INTO saved_items ({columns}) VALUES ({params})"), row)
    assert excinfo.value.orig.sqlstate == "23514"


def test_updated_at_may_not_precede_created_at(clean_db: Engine) -> None:
    with pytest.raises(IntegrityError), clean_db.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO saved_items (id, user_id, content_id, created_at, updated_at) "
                "VALUES (:id, :u, 'c', now(), now() - interval '1 second')"
            ),
            {"id": uuid.uuid4(), "u": DEV_USER_ID},
        )


def test_f034_only_one_local_seed_user_can_exist(clean_db: Engine) -> None:
    with pytest.raises(IntegrityError) as excinfo, clean_db.begin() as connection:
        connection.execute(text("INSERT INTO users (id) VALUES (:id)"), {"id": uuid.uuid4()})
    assert excinfo.value.orig.diag.constraint_name == "uq_users_local_seed"


def test_external_identity_requires_issuer_and_subject_together(clean_db: Engine) -> None:
    with pytest.raises(IntegrityError) as excinfo, clean_db.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, issuer) VALUES (:id, 'https://idp')"),
            {"id": uuid.uuid4()},
        )
    assert excinfo.value.orig.diag.constraint_name == "ck_external_identity_pair"


def test_owner_with_saved_items_cannot_be_deleted(clean_db: Engine) -> None:
    _create(clean_db)
    with pytest.raises(IntegrityError), clean_db.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": DEV_USER_ID})
    assert _count(clean_db) == 1


def test_f024_migrations_rebuild_from_a_clean_database(
    alembic_config: Config, clean_db: Engine
) -> None:
    command.downgrade(alembic_config, "base")
    with clean_db.connect() as connection:
        tables = connection.execute(
            text("SELECT count(*) FROM pg_tables WHERE tablename IN ('users', 'saved_items')")
        ).scalar_one()
    assert tables == 0
    command.upgrade(alembic_config, "head")
    with clean_db.connect() as connection:
        seed = connection.execute(text("SELECT id, issuer, subject FROM users")).all()
    assert [tuple(row) for row in seed] == [(DEV_USER_ID, None, None)]
