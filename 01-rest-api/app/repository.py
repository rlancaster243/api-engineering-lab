"""Owner-scoped persistence for saved items (spec sections 5.2 and 6, ADR 0003).

Every statement carries ``user_id`` in its WHERE clause, and every mutation is a single
statement, so ownership check and change cannot be separated by a concurrent request.
Each method commits before returning: a caller never reports success for a write that
is not durable.
"""

import uuid

from sqlalchemy import Row, case, delete, func, insert, literal, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import SavedItem
from app.pagination import CursorPosition

_items = SavedItem.__table__
_c = _items.c

# Exactly the columns a client may see; user_id never leaves this module.
RESPONSE_COLUMNS = (
    _c.id,
    _c.content_id,
    _c.title,
    _c.url,
    _c.notes,
    _c.created_at,
    _c.updated_at,
)
MUTABLE_FIELDS = ("title", "url", "notes")

UNIQUE_VIOLATION = "23505"
DUPLICATE_CONSTRAINT = "uq_user_content"


class DuplicateItem(Exception):
    """The owner already saved this content_id."""


def is_duplicate_save(exc: IntegrityError) -> bool:
    """Only the owner/content unique constraint means "already saved".

    A primary-key collision is also SQLSTATE 23505, and FK or CHECK violations are other
    integrity errors; reporting any of them as 409 would tell the client the content is
    saved when it is not.
    """
    orig = exc.orig
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    return getattr(orig, "sqlstate", None) == UNIQUE_VIOLATION and constraint == (
        DUPLICATE_CONSTRAINT
    )


class ItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        content_id: str,
        title: str | None,
        url: str | None,
        notes: str | None,
    ) -> Row:
        # No timestamps: both columns default to the database's now().
        statement = (
            insert(_items)
            .values(
                id=item_id,
                user_id=user_id,
                content_id=content_id,
                title=title,
                url=url,
                notes=notes,
            )
            .returning(*RESPONSE_COLUMNS)
        )
        try:
            row = self.session.execute(statement).one()
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            if is_duplicate_save(exc):
                raise DuplicateItem from exc
            raise
        return row

    def get(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> Row | None:
        statement = select(*RESPONSE_COLUMNS).where(_c.id == item_id, _c.user_id == user_id)
        return self.session.execute(statement).one_or_none()

    def list_page(
        self, *, user_id: uuid.UUID, limit: int, after: CursorPosition | None
    ) -> list[Row]:
        """Up to ``limit + 1`` rows; the extra row only signals that a next page exists."""
        statement = select(*RESPONSE_COLUMNS).where(_c.user_id == user_id)
        if after is not None:
            # Row-value comparison: strictly later in (created_at DESC, id DESC) order.
            statement = statement.where(
                tuple_(_c.created_at, _c.id) < tuple_(after.created_at, after.item_id)
            )
        statement = statement.order_by(_c.created_at.desc(), _c.id.desc()).limit(limit + 1)
        return list(self.session.execute(statement).all())

    def update(
        self, *, user_id: uuid.UUID, item_id: uuid.UUID, changes: dict[str, str | None]
    ) -> Row | None:
        """Apply ``changes`` in one owner-scoped statement.

        The row matches whenever the caller owns it, so ``None`` always means "not found",
        never "nothing changed". ``updated_at`` moves only when a value actually differs.
        Inside UPDATE, column references on the right-hand side read the *old* row.
        """
        new_values = {
            name: literal(changes[name], type_=_c[name].type) if name in changes else _c[name]
            for name in MUTABLE_FIELDS
        }
        changed = tuple_(*(_c[name] for name in MUTABLE_FIELDS)).is_distinct_from(
            tuple_(*(new_values[name] for name in MUTABLE_FIELDS))
        )
        statement = (
            update(_items)
            .where(_c.id == item_id, _c.user_id == user_id)
            .values(**new_values, updated_at=case((changed, func.now()), else_=_c.updated_at))
            .returning(*RESPONSE_COLUMNS)
        )
        row = self.session.execute(statement).one_or_none()
        self.session.commit()
        return row

    def delete(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> bool:
        statement = delete(_items).where(_c.id == item_id, _c.user_id == user_id).returning(_c.id)
        deleted = self.session.execute(statement).one_or_none()
        self.session.commit()
        return deleted is not None
