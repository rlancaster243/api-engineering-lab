"""Asset routes."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.auth import CurrentTenant
from app.db import get_connection
from app.schemas import (
    DEFAULT_LIMIT,
    AssetCreate,
    AssetDetail,
    AssetPage,
    BatchCreate,
    BatchSummary,
    Limit,
    TagCreate,
)
from app.service import AssetRegistry

router = APIRouter(prefix="/v1/assets", tags=["assets"])
batch_router = APIRouter(prefix="/v1/batch", tags=["batch"])


def get_registry(
    request: Request,
    tenant: CurrentTenant,
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> AssetRegistry:
    return AssetRegistry(connection, tenant.id)


Registry = Annotated[AssetRegistry, Depends(get_registry)]


@router.post("", response_model=AssetDetail, status_code=201)
def create_asset(body: AssetCreate, registry: Registry) -> AssetDetail:
    return registry.create(body)


@router.get("", response_model=AssetPage)
def list_assets(registry: Registry, response: Response, limit: Limit = DEFAULT_LIMIT) -> AssetPage:
    response.headers["X-Applied-Limit"] = str(limit)
    return AssetPage(assets=registry.list_page(limit))


@router.get("/{asset_id}", response_model=AssetDetail)
def get_asset(asset_id: str, registry: Registry) -> AssetDetail:
    return registry.get(asset_id)


@router.post("/{asset_id}/tags", response_model=AssetDetail)
def add_tags(asset_id: str, body: TagCreate, registry: Registry) -> AssetDetail:
    return registry.add_tags(asset_id, body)


@batch_router.post("/reprocess", response_model=BatchSummary)
def reprocess_batch(body: BatchCreate, registry: Registry) -> BatchSummary:
    return registry.reprocess(body.ids)
