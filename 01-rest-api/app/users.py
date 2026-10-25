"""Map verified external principals to internal user rows (spec section 5, PR-06).

A verified token yields ``(issuer, subject)``; the API stores opaque internal UUIDs.
Resolution is read-mostly: a SELECT on the hot path avoids taking a row lock on every
authenticated GET. The first sight of a new principal uses an upsert so concurrent first
requests still converge on one row.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(self, issuer: str, subject: str) -> uuid.UUID:
        existing = self.session.scalar(
            select(User.id).where(User.issuer == issuer, User.subject == subject)
        )
        if existing is not None:
            return existing

        new_id = uuid.uuid4()
        statement = (
            pg_insert(User)
            .values(id=new_id, issuer=issuer, subject=subject)
            .on_conflict_do_update(
                constraint="uq_external_identity",
                set_={"issuer": issuer},
            )
            .returning(User.id)
        )
        user_id = self.session.scalar(statement)
        if user_id is None:
            raise RuntimeError("user upsert returned no id")
        self.session.commit()
        return user_id
