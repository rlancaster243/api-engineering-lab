"""Saved-item use cases: owner scope in, contract errors out (spec section 2)."""

import uuid

from sqlalchemy import Row
from sqlalchemy.orm import Session

from app.errors import ApiError, ErrorCode
from app.pagination import CursorCodec, CursorPosition, InvalidCursor
from app.repository import DuplicateItem, ItemRepository
from app.schemas import ItemCreate, ItemPage, ItemPatch, ItemResponse


def _to_response(row: Row) -> ItemResponse:
    return ItemResponse.model_validate(dict(row._mapping))


class ItemService:
    def __init__(self, session: Session, codec: CursorCodec, user_id: uuid.UUID) -> None:
        self.repository = ItemRepository(session)
        self.codec = codec
        self.user_id = user_id

    def create(self, body: ItemCreate) -> ItemResponse:
        try:
            row = self.repository.create(
                user_id=self.user_id,
                item_id=uuid.uuid4(),
                content_id=body.content_id,
                title=body.title,
                url=body.url,
                notes=body.notes,
            )
        except DuplicateItem:
            raise ApiError(ErrorCode.ITEM_ALREADY_SAVED) from None
        return _to_response(row)

    def get(self, item_id: uuid.UUID) -> ItemResponse:
        row = self.repository.get(user_id=self.user_id, item_id=item_id)
        if row is None:
            # Missing and owned-by-someone-else are indistinguishable by design.
            raise ApiError(ErrorCode.ITEM_NOT_FOUND)
        return _to_response(row)

    def list_page(self, limit: int, cursor: str | None) -> ItemPage:
        after = None
        if cursor is not None:
            try:
                after = self.codec.decode(cursor, self.user_id)
            except InvalidCursor:
                raise ApiError(ErrorCode.INVALID_CURSOR) from None
        rows = self.repository.list_page(user_id=self.user_id, limit=limit, after=after)
        page, extra = rows[:limit], rows[limit:]
        next_cursor = None
        if extra:
            last = page[-1]
            next_cursor = self.codec.encode(
                CursorPosition(created_at=last.created_at, item_id=last.id), self.user_id
            )
        return ItemPage(items=[_to_response(row) for row in page], next_cursor=next_cursor)

    def update(self, item_id: uuid.UUID, body: ItemPatch) -> ItemResponse:
        row = self.repository.update(user_id=self.user_id, item_id=item_id, changes=body.changes())
        if row is None:
            raise ApiError(ErrorCode.ITEM_NOT_FOUND)
        return _to_response(row)

    def delete(self, item_id: uuid.UUID) -> None:
        if not self.repository.delete(user_id=self.user_id, item_id=item_id):
            raise ApiError(ErrorCode.ITEM_NOT_FOUND)
