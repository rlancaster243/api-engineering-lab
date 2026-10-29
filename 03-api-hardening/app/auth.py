"""Caller identity: an ``X-API-Key`` header resolved to a tenant.

Keys are stored as SHA-256 digests, so the table holds no usable credential. The
lookup is by digest, which is a primary-key probe and cannot be used to enumerate
keys. There is no unauthenticated fallback: a missing or unknown key is a 401 on
every route except the probes.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.config import LOCAL_ENVS, Settings
from app.db import get_connection
from app.errors import ApiError, ErrorCode

API_KEY_HEADER = "X-API-Key"

# Only for APP_ENV=local, so the two sample tenants in the README are usable with curl.
DEV_API_KEYS = {
    "local-dev-key-alpha": "Tenant Alpha",
    "local-dev-key-beta": "Tenant Beta",
}


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def seed_local_tenants(connection: sqlite3.Connection) -> None:
    """Create the two documentation tenants once, in local development only."""
    for index, (api_key, name) in enumerate(DEV_API_KEYS.items(), start=1):
        tenant_id = f"tenant-{index}"
        connection.execute(
            "INSERT OR IGNORE INTO tenants (id, name) VALUES (?, ?)", (tenant_id, name)
        )
        connection.execute(
            "INSERT OR IGNORE INTO api_keys (key_hash, tenant_id, label) VALUES (?, ?, ?)",
            (hash_key(api_key), tenant_id, "local development"),
        )
    connection.commit()


def _presented_key(request: Request) -> str:
    api_key = request.headers.get(API_KEY_HEADER, "").strip()
    if not api_key:
        raise ApiError(ErrorCode.UNAUTHENTICATED)
    return api_key


def current_tenant(
    request: Request,
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> Tenant:
    row = connection.execute(
        """
        SELECT tenants.id, tenants.name
        FROM api_keys
        JOIN tenants ON tenants.id = api_keys.tenant_id
        WHERE api_keys.key_hash = ?
        """,
        (hash_key(_presented_key(request)),),
    ).fetchone()
    if row is None:
        raise ApiError(ErrorCode.UNAUTHENTICATED)
    return Tenant(id=row["id"], name=row["name"])


CurrentTenant = Annotated[Tenant, Depends(current_tenant)]


def local_env(settings: Settings) -> bool:
    """Whether the seeded documentation tenants belong in this environment.

    Takes the already-loaded ``Settings`` rather than reading ``os.environ`` again: the
    environment is interpreted once, at startup, so this answer cannot contradict the
    configuration the rest of the process is using.
    """
    return settings.app_env in LOCAL_ENVS
