"""Request and response models, and the limits the registry enforces."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Query
from pydantic import BaseModel, Field, field_validator

NAME_MAX = 200
SOURCE_URI_MAX = 2048
CONTENT_TYPE_MAX = 120
TAG_MAX = 50
MAX_TAGS_PER_ASSET = 12
MAX_BATCH_SIZE = 50
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_SIZE_BYTES = 2**63 - 1


class AssetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    source_uri: str = Field(min_length=1, max_length=SOURCE_URI_MAX)
    content_type: str | None = Field(default=None, max_length=CONTENT_TYPE_MAX)
    size_bytes: int | None = Field(default=None, ge=0, le=MAX_SIZE_BYTES)

    @field_validator("name")
    @classmethod
    def _name_is_present(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must contain something other than whitespace")
        return stripped

    @field_validator("source_uri")
    @classmethod
    def _source_uri_is_absolute_http(cls, value: str) -> str:
        stripped = value.strip()
        parts = urlsplit(stripped)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("source_uri must be an absolute http or https URL")
        return stripped


class TagCreate(BaseModel):
    tags: list[str] = Field(min_length=1, max_length=MAX_TAGS_PER_ASSET)

    @field_validator("tags")
    @classmethod
    def _tags_are_usable(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            tag = raw.strip()
            if not tag:
                raise ValueError("tags must not be blank")
            if len(tag) > TAG_MAX:
                raise ValueError(f"tags must be at most {TAG_MAX} characters")
            if not tag.replace("-", "").replace("_", "").replace(".", "").isalnum():
                raise ValueError("tags may contain letters, digits, '.', '-' and '_' only")
            if tag not in cleaned:
                cleaned.append(tag)
        return cleaned


class BatchCreate(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class AssetSummary(BaseModel):
    id: str
    name: str
    source_uri: str
    content_type: str | None
    size_bytes: int | None
    checksum: str
    metadata_version: int
    created_at: str
    updated_at: str


class AssetDetail(AssetSummary):
    tags: list[str]


class AssetPage(BaseModel):
    assets: list[AssetSummary]


class BatchItem(BaseModel):
    id: str
    status: Literal["reprocessed", "skipped"]
    reason: str | None = None


class BatchSummary(BaseModel):
    processed: int
    failed: int
    results: list[BatchItem]


Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
