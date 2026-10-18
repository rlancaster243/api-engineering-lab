import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import DEV_USER_ID, SavedItem
from app.repository import DuplicateSavedItemError, ItemRepository


class ItemNotFoundError(Exception):
    pass


class ItemConflictError(Exception):
    pass


class ItemService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = ItemRepository(session)

    def create(self, content_id: str, notes: str | None) -> SavedItem:
        now = datetime.now(UTC)
        item = SavedItem(
            id=uuid.uuid4(),
            user_id=DEV_USER_ID,
            content_id=content_id,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        try:
            self.repository.add(item)
        except DuplicateSavedItemError as exc:
            raise ItemConflictError from exc
        self.session.commit()
        self.session.refresh(item)
        return item

    def get(self, item_id: uuid.UUID) -> SavedItem:
        item = self.repository.get(item_id, DEV_USER_ID)
        if item is None:
            raise ItemNotFoundError
        return item

    def list_items(self, limit: int, offset: int) -> list[SavedItem]:
        return self.repository.list_for_user(DEV_USER_ID, limit, offset)

    def update(self, item_id: uuid.UUID, notes: str | None, notes_set: bool) -> SavedItem:
        item = self.get(item_id)
        if notes_set:
            item.notes = notes
            item.updated_at = datetime.now(UTC)
            self.session.commit()
            self.session.refresh(item)
        return item

    def delete(self, item_id: uuid.UUID) -> None:
        item = self.get(item_id)
        self.repository.delete(item)
        self.session.commit()
