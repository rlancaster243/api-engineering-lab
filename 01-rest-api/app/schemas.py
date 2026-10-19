"""Request and response schemas for ``/v1/items`` (spec sections 4.1-4.4, ADR 0003).

These define the wire contract only. Persistence, ownership and routing live elsewhere;
nothing here knows about ``user_id``, which API clients can never supply.
"""

import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Query
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    field_serializer,
    field_validator,
    model_validator,
)

# The exact set the database CHECK constraints trim (spec section 3.2). Python's default
# str.strip() also removes other Unicode whitespace and would disagree with the database.
TRIM_CHARS = " \t\n\r\f\v"

CONTENT_ID_MAX = 255
TITLE_MAX = 200
URL_MAX = 2048
NOTES_MAX = 2000

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# Reusable by the list route: out-of-range or non-integer values are a 422, never clamped.
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]


def _is_blank(value: str) -> bool:
    return value.strip(TRIM_CHARS) == ""


def _check_title(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > TITLE_MAX:
        raise ValueError(f"title must be at most {TITLE_MAX} characters")
    if _is_blank(value):
        raise ValueError("title must not be blank")
    return value


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > URL_MAX:
        raise ValueError(f"url must be at most {URL_MAX} characters")
    if any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("url must not contain whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be an absolute http(s) URL")
    return value


def _check_notes(value: str | None) -> str | None:
    if value is not None and len(value) > NOTES_MAX:
        raise ValueError(f"notes must be at most {NOTES_MAX} characters")
    return value


def normalize_content_id(value: str) -> str:
    """Trim the section 4.2 set, then apply Unicode NFC.

    Without NFC, canonically equivalent strings (e.g. precomposed vs combining accents)
    would be distinct saved items. Comparison afterwards is exact and case-sensitive.
    """
    normalized = unicodedata.normalize("NFC", value.strip(TRIM_CHARS))
    if not normalized:
        raise ValueError("content_id must not be blank")
    if len(normalized) > CONTENT_ID_MAX:
        raise ValueError(f"content_id must be at most {CONTENT_ID_MAX} characters")
    return normalized


class ItemCreate(BaseModel):
    """POST /v1/items body. Unknown fields, including ``user_id`` and ``id``, are rejected."""

    model_config = ConfigDict(extra="forbid")

    content_id: str
    title: str | None = None
    url: str | None = None
    notes: str | None = None

    _content_id = field_validator("content_id")(normalize_content_id)
    _title = field_validator("title")(_check_title)
    _url = field_validator("url")(_check_url)
    _notes = field_validator("notes")(_check_notes)


class ItemPatch(BaseModel):
    """PATCH /v1/items/{id} body.

    Omitted fields are unchanged; an explicit ``null`` clears the field. The two are
    distinguished through ``model_fields_set``, so a field's default value cannot be used
    to tell them apart. ``content_id``, ``user_id`` and ``id`` are rejected as unknown.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    url: str | None = None
    notes: str | None = None

    _title = field_validator("title")(_check_title)
    _url = field_validator("url")(_check_url)
    _notes = field_validator("notes")(_check_notes)

    @model_validator(mode="after")
    def _require_a_field(self) -> "ItemPatch":
        if not self.model_fields_set:
            raise ValueError("at least one of title, url, notes is required")
        return self

    def changes(self) -> dict[str, str | None]:
        """Only the fields the client actually sent, including explicit nulls."""
        return {name: getattr(self, name) for name in self.model_fields_set}


class ItemResponse(BaseModel):
    """A saved item as returned to its owner. ``user_id`` is never exposed."""

    id: uuid.UUID
    content_id: str
    title: str | None
    url: str | None
    notes: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_serializer("created_at", "updated_at")
    def _serialize_timestamp(self, value: datetime) -> str:
        # UTC with a trailing Z; microseconds appear only when non-zero.
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ItemPage(BaseModel):
    """GET /v1/items response. ``next_cursor`` is null when no further row exists."""

    items: list[ItemResponse]
    next_cursor: str | None
