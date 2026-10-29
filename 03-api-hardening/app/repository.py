"""Tenant-scoped persistence for the asset registry.

Statements carry the tenant in their WHERE clause, so which rows a caller can reach
is decided by the database rather than by a comparison in Python after a row has
already been loaded into the process.
"""

import sqlite3
import uuid
from datetime import UTC, datetime

ASSET_COLUMNS = (
    "id, name, source_uri, content_type, size_bytes, "
    "checksum, metadata_version, created_at, updated_at"
)


class AssetNotFound(Exception):
    """The asset does not exist, or does not belong to the calling tenant."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class AssetRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(
        self,
        *,
        tenant_id: str,
        name: str,
        source_uri: str,
        content_type: str | None,
        size_bytes: int | None,
        checksum: str,
    ) -> sqlite3.Row:
        asset_id = str(uuid.uuid4())
        timestamp = now_iso()
        self.connection.execute(
            "INSERT INTO assets (id, tenant_id, name, source_uri, content_type, size_bytes,"
            " checksum, metadata_version, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                asset_id,
                tenant_id,
                name,
                source_uri,
                content_type,
                size_bytes,
                checksum,
                timestamp,
                timestamp,
            ),
        )
        self.connection.commit()
        row = self.get_by_id(tenant_id, asset_id)
        assert row is not None
        return row

    def get_by_id(self, tenant_id: str, asset_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT {ASSET_COLUMNS} FROM assets WHERE id = ? AND tenant_id = ?",
            (asset_id, tenant_id),
        ).fetchone()

    def list_for_tenant(self, tenant_id: str, limit: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                f"SELECT {ASSET_COLUMNS} FROM assets WHERE tenant_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (tenant_id, limit),
            )
        )

    def tags_for(self, tenant_id: str, asset_id: str) -> list[str]:
        return [
            row["tag"]
            for row in self.connection.execute(
                "SELECT t.tag FROM asset_tags t"
                " INNER JOIN assets a ON a.id = t.asset_id"
                " WHERE t.asset_id = ? AND a.tenant_id = ?"
                " ORDER BY t.tag",
                (asset_id, tenant_id),
            )
        ]

    def add_tags(self, tenant_id: str, asset_id: str, tags: list[str]) -> list[str]:
        """Store the tags the asset does not already carry, then return all of them."""
        if not self._visible(tenant_id, asset_id):
            raise AssetNotFound(asset_id)
        timestamp = now_iso()
        self.connection.executemany(
            "INSERT OR IGNORE INTO asset_tags (asset_id, tag, created_at) VALUES (?, ?, ?)",
            [(asset_id, tag, timestamp) for tag in tags],
        )
        self.connection.execute(
            "UPDATE assets SET updated_at = ? WHERE id = ? AND tenant_id = ?",
            (timestamp, asset_id, tenant_id),
        )
        self.connection.commit()
        return self.tags_for(tenant_id, asset_id)

    def declared_metadata(self, tenant_id: str, asset_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT source_uri, content_type, size_bytes FROM assets"
            " WHERE id = ? AND tenant_id = ?",
            (asset_id, tenant_id),
        ).fetchone()

    def record_reprocess(self, tenant_id: str, asset_id: str, checksum: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE assets SET checksum = ?, metadata_version = metadata_version + 1,"
            " updated_at = ? WHERE id = ? AND tenant_id = ?",
            (checksum, now_iso(), asset_id, tenant_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def _visible(self, tenant_id: str, asset_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM assets WHERE id = ? AND tenant_id = ?", (asset_id, tenant_id)
        ).fetchone()
        return row is not None
