"""User principal resolution (PR-06)."""

from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from app.users import UserRepository


def test_same_subject_different_issuers_are_distinct_users(clean_db: Engine) -> None:
    session = sessionmaker(bind=clean_db, autoflush=False, autocommit=False)()
    try:
        repo = UserRepository(session)
        shared = "shared-subject"
        id_a = repo.resolve("https://issuer-a.test", shared)
        id_b = repo.resolve("https://issuer-b.test", shared)
        assert id_a != id_b
        again = repo.resolve("https://issuer-a.test", shared)
        assert again == id_a
    finally:
        session.close()

    with clean_db.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM users WHERE subject = :sub"),
            {"sub": shared},
        ).scalar_one()
    assert count == 2


def test_repeat_resolve_does_not_insert_again(clean_db: Engine) -> None:
    session = sessionmaker(bind=clean_db, autoflush=False, autocommit=False)()
    try:
        repo = UserRepository(session)
        user_id = repo.resolve("https://issuer.test", "stable")
        with clean_db.connect() as connection:
            xmin_before = connection.execute(
                text("SELECT xmin FROM users WHERE id = :id"),
                {"id": user_id},
            ).scalar_one()
        assert repo.resolve("https://issuer.test", "stable") == user_id
        with clean_db.connect() as connection:
            xmin_after = connection.execute(
                text("SELECT xmin FROM users WHERE id = :id"),
                {"id": user_id},
            ).scalar_one()
    finally:
        session.close()
    assert xmin_before == xmin_after
