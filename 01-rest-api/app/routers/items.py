import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.auth import current_user_id
from app.db import get_session
from app.schemas import DEFAULT_LIMIT, ItemCreate, ItemPage, ItemPatch, ItemResponse, Limit
from app.service import ItemService

router = APIRouter(prefix="/v1/items", tags=["items"])


def get_service(
    request: Request,
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
    session: Annotated[Session, Depends(get_session)],
) -> ItemService:
    return ItemService(session, request.app.state.cursor_codec, user_id)


Service = Annotated[ItemService, Depends(get_service)]


@router.post("", response_model=ItemResponse, status_code=201)
def create_item(body: ItemCreate, service: Service) -> ItemResponse:
    return service.create(body)


@router.get("", response_model=ItemPage)
def list_items(
    service: Service, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> ItemPage:
    return service.list_page(limit, cursor)


@router.get("/{item_id}", response_model=ItemResponse)
def get_item(item_id: uuid.UUID, service: Service) -> ItemResponse:
    return service.get(item_id)


@router.patch("/{item_id}", response_model=ItemResponse)
def update_item(item_id: uuid.UUID, body: ItemPatch, service: Service) -> ItemResponse:
    return service.update(item_id, body)


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: uuid.UUID, service: Service) -> Response:
    service.delete(item_id)
    return Response(status_code=204)
