import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ItemCreate, ItemListResponse, ItemResponse, ItemUpdate
from app.service import ItemConflictError, ItemNotFoundError, ItemService

router = APIRouter(prefix="/v1/items", tags=["items"])


def get_service(session: Session = Depends(get_db)) -> ItemService:
    return ItemService(session)


@router.post("", response_model=ItemResponse, status_code=201)
def create_item(body: ItemCreate, service: ItemService = Depends(get_service)) -> ItemResponse:
    try:
        item = service.create(body.content_id, body.notes)
    except ItemConflictError:
        raise HTTPException(status_code=409, detail="Item already saved") from None
    return ItemResponse.model_validate(item)


@router.get("", response_model=ItemListResponse)
def list_items(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: ItemService = Depends(get_service),
) -> ItemListResponse:
    items = service.list_items(limit, offset)
    return ItemListResponse(
        items=[ItemResponse.model_validate(item) for item in items],
        limit=limit,
        offset=offset,
    )


@router.get("/{item_id}", response_model=ItemResponse)
def get_item(item_id: uuid.UUID, service: ItemService = Depends(get_service)) -> ItemResponse:
    try:
        item = service.get(item_id)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found") from None
    return ItemResponse.model_validate(item)


@router.patch("/{item_id}", response_model=ItemResponse)
def update_item(
    item_id: uuid.UUID,
    body: ItemUpdate,
    service: ItemService = Depends(get_service),
) -> ItemResponse:
    try:
        item = service.update(item_id, body.notes, "notes" in body.model_fields_set)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found") from None
    return ItemResponse.model_validate(item)


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: uuid.UUID, service: ItemService = Depends(get_service)) -> Response:
    try:
        service.delete(item_id)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found") from None
    return Response(status_code=204)
