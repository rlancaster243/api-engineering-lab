import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from pydantic import BaseModel

from app.auth import local_env, seed_local_tenants
from app.config import load_settings
from app.db import connect, initialize
from app.errors import ApiError, ErrorCode, install_error_handling
from app.routers.assets import batch_router
from app.routers.assets import router as assets_router


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str


def prepare_database(database_path: str) -> None:
    """Create the schema and, in local development, the documentation tenants."""
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database_path)
    try:
        initialize(connection)
        if local_env():
            seed_local_tenants(connection)
    finally:
        connection.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail closed: a deployment without a database path stops here rather than
    # creating a file next to the process working directory. Opening a connection does
    # not read a row, so /health stays a liveness check that does not depend on data.
    settings = load_settings()
    prepare_database(settings.database_path)
    app.state.settings = settings
    yield


app = FastAPI(
    title="Asset Registry",
    description="Phase 3: API hardening and production patterns",
    version="0.1.0",
    lifespan=lifespan,
)
install_error_handling(app)
app.include_router(assets_router)
app.include_router(batch_router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/ready", response_model=ReadyResponse)
def ready(request: Request) -> ReadyResponse:
    try:
        connection = connect(request.app.state.settings.database_path)
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        # A probe must never answer 5xx with storage detail: it is scraped continuously
        # and its body lands in orchestration logs.
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE) from None
    return ReadyResponse(status="ready")
