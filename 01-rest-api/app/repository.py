import uuid

from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import SavedItem


class DuplicateSavedItemError(Exception):
    pass


class ItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, item: SavedItem) -> SavedItem:
        self.session.add(item)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            if isinstance(exc.orig, UniqueViolation):
                raise DuplicateSavedItemError from exc
            raise
        return item

    def get(self, item_id: uuid.UUID, user_id: uuid.UUID) -> SavedItem | None:
        statement = select(SavedItem).where(SavedItem.id == item_id, SavedItem.user_id == user_id)
        return self.session.scalar(statement)

    def list_for_user(self, user_id: uuid.UUID, limit: int, offset: int) -> list[SavedItem]:
        statement = (
            select(SavedItem)
            .where(SavedItem.user_id == user_id)
            .order_by(SavedItem.created_at.desc(), SavedItem.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def delete(self, item: SavedItem) -> None:
        self.session.delete(item)
        self.session.flush()
