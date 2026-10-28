"""SQLite connection handling.

One connection per request, opened by a FastAPI dependency and closed when the
request ends. A connection is never shared between requests, so ``check_same_thread``
is disabled only to let the threadpool hand a request's own connection between the
dependency and the endpoint that runs it.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from fastapi import Request

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash  TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    label     TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    source_uri       TEXT NOT NULL,
    content_type     TEXT,
    size_bytes       INTEGER,
    checksum         TEXT NOT NULL,
    metadata_version INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_assets_tenant_created
    ON assets (tenant_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id   TEXT NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, tag)
);
"""


def connect(database_path: str) -> sqlite3.Connection:
    """Open a connection with row access by name and foreign keys enforced."""
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """Create the schema if it is absent; safe to run on every start."""
    with closing(connection.cursor()) as cursor:
        cursor.executescript(SCHEMA)
    connection.commit()


def get_connection(request: Request) -> Iterator[sqlite3.Connection]:
    connection = connect(request.app.state.settings.database_path)
    try:
        yield connection
    finally:
        connection.close()
