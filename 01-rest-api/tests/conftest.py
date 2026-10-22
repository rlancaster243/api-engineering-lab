"""Test-only stub app that mounts the real schemas and error handlers.

PR-01 ships the contract (schemas, error envelope, cursor) without real ``/v1/items``
routes; those arrive with persistence in PR-03/PR-04. These stub routes exist only so the
contract can be exercised over HTTP, and return canned data.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import ApiError, ErrorCode, install_error_handling
from app.schemas import (
    DEFAULT_LIMIT,
    ItemCreate,
    ItemPage,
    ItemPatch,
    ItemResponse,
    Limit,
)

_NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)


def build_contract_app() -> FastAPI:
    app = FastAPI()
    install_error_handling(app)

    @app.post("/v1/items", status_code=201, response_model=ItemResponse)
    def create_item(body: ItemCreate) -> ItemResponse:
        return ItemResponse(id=uuid.uuid4(), created_at=_NOW, updated_at=_NOW, **body.model_dump())

    @app.get("/v1/items", response_model=ItemPage)
    def list_items(
        response: Response, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
    ) -> ItemPage:
        response.headers["X-Applied-Limit"] = str(limit)
        return ItemPage(items=[], next_cursor=None)

    @app.get("/v1/items/{item_id}")
    def get_item(item_id: uuid.UUID) -> dict[str, str]:
        return {"id": str(item_id)}

    @app.patch("/v1/items/{item_id}")
    def patch_item(item_id: uuid.UUID, body: ItemPatch) -> JSONResponse:
        return JSONResponse(body.changes())

    @app.delete("/v1/items/{item_id}", status_code=204)
    def delete_item(item_id: uuid.UUID) -> None:
        return None

    @app.get("/_raise/{code}")
    def raise_api_error(code: ErrorCode) -> None:
        raise ApiError(code)

    @app.get("/_boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/_http/{status}")
    def raise_http(status: int) -> None:
        raise StarletteHTTPException(status_code=status, detail="secret internal detail")

    return app


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so an unhandled exception is rendered by the
    # installed handler, as a real server would, instead of re-raised into the test.
    return TestClient(build_contract_app(), raise_server_exceptions=False)
