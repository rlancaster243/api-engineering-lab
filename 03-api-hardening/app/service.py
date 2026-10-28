"""Asset registry use cases: tenant scope in, contract errors out.

The service owns the transaction boundaries. Repository methods each commit what they
write, so an operation that spans two of them is not atomic; that is called out where
it happens below.
"""

import hashlib
import sqlite3

from app.errors import ApiError, ErrorCode
from app.repository import AssetNotFound, AssetRepository
from app.schemas import (
    AssetCreate,
    AssetDetail,
    AssetSummary,
    BatchItem,
    BatchSummary,
    TagCreate,
)


def derive_checksum(source_uri: str, content_type: str | None, size_bytes: int | None) -> str:
    """A stable fingerprint of the declared metadata, used to notice re-publication."""
    canonical = (
        f"{source_uri}\x1f{content_type or ''}\x1f{'' if size_bytes is None else size_bytes}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _summary(row: sqlite3.Row) -> AssetSummary:
    return AssetSummary(
        id=row["id"],
        name=row["name"],
        source_uri=row["source_uri"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        checksum=row["checksum"],
        metadata_version=row["metadata_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class AssetRegistry:
    def __init__(self, connection: sqlite3.Connection, tenant_id: str) -> None:
        self.repository = AssetRepository(connection)
        self.tenant_id = tenant_id

    def create(self, body: AssetCreate) -> AssetDetail:
        row = self.repository.create(
            tenant_id=self.tenant_id,
            name=body.name,
            source_uri=body.source_uri,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            checksum=derive_checksum(body.source_uri, body.content_type, body.size_bytes),
        )
        return AssetDetail(**_summary(row).model_dump(), tags=[])

    def get(self, asset_id: str) -> AssetDetail:
        """The detail view a client opens after following a link to an asset."""
        row = self.repository.get_by_id(asset_id)
        if row is None:
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        return AssetDetail(**_summary(row).model_dump(), tags=self.repository.tags_for(asset_id))

    def list_page(self, limit: int) -> list[AssetSummary]:
        return [_summary(row) for row in self.repository.list_for_tenant(self.tenant_id, limit)]

    def add_tags(self, asset_id: str, body: TagCreate) -> AssetDetail:
        try:
            self.repository.add_tags(self.tenant_id, asset_id, body.tags)
        except AssetNotFound:
            raise ApiError(ErrorCode.ASSET_NOT_FOUND) from None
        # The parent's timestamp is advanced after the tag rows land, so a tag write is
        # never invisible to a client polling the asset.
        self.repository.touch(self.tenant_id, asset_id)
        row = self.repository.get_by_id(asset_id)
        assert row is not None
        return AssetDetail(**_summary(row).model_dump(), tags=self.repository.tags_for(asset_id))

    def reprocess(self, ids: list[str]) -> BatchSummary:
        """Re-derive the stored fingerprint for each asset the caller owns."""
        results: list[BatchItem] = []
        processed = 0
        failed = 0
        for asset_id in ids:
            declared = self.repository.declared_metadata(self.tenant_id, asset_id)
            if declared is None:
                failed += 1
                results.append(BatchItem(id=asset_id, status="skipped", reason="not_found"))
                continue
            checksum = derive_checksum(
                declared["source_uri"], declared["content_type"], declared["size_bytes"]
            )
            if not self.repository.record_reprocess(self.tenant_id, asset_id, checksum):
                failed += 1
                results.append(BatchItem(id=asset_id, status="skipped", reason="not_writable"))
                continue
            processed += 1
            results.append(BatchItem(id=asset_id, status="reprocessed"))
        return BatchSummary(processed=processed, failed=failed, results=results)
