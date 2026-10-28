"""Shared fixtures.

Every test runs against a real SQLite file in a per-test temporary directory, so the
schema, the constraints and the commit boundaries are the same ones production uses.
There is no in-memory shortcut: a test that passed against a fake repository would say
nothing about the statements the service actually runs.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_key
from app.db import connect, initialize

ALPHA_KEY = "test-key-alpha"
BETA_KEY = "test-key-beta"
ALPHA_TENANT = "tenant-alpha"
BETA_TENANT = "tenant-beta"

ASSET_BODY = {
    "name": "Quarterly report",
    "source_uri": "https://files.example.test/reports/q3.pdf",
    "content_type": "application/pdf",
    "size_bytes": 4096,
}


def seed_tenants(connection: sqlite3.Connection) -> None:
    for tenant_id, name, key in (
        (ALPHA_TENANT, "Alpha", ALPHA_KEY),
        (BETA_TENANT, "Beta", BETA_KEY),
    ):
        connection.execute("INSERT INTO tenants (id, name) VALUES (?, ?)", (tenant_id, name))
        connection.execute(
            "INSERT INTO api_keys (key_hash, tenant_id, label) VALUES (?, ?, ?)",
            (hash_key(key), tenant_id, "test"),
        )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "registry.sqlite3"
    connection = connect(str(path))
    try:
        initialize(connection)
        seed_tenants(connection)
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture
def connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    """A direct connection for arranging rows and asserting stored state."""
    conn = connect(str(database_path))
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("APP_ENV", "test")

    from app.main import app

    # The context manager runs the lifespan, i.e. real startup and shutdown.
    with TestClient(app) as test_client:
        yield test_client


def auth(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def create_asset(client: TestClient, api_key: str = ALPHA_KEY, **overrides: object) -> dict:
    body = ASSET_BODY | overrides
    response = client.post("/v1/assets", json=body, headers=auth(api_key))
    assert response.status_code == 201, response.text
    return response.json()
