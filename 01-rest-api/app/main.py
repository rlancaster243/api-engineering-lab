from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import load_settings
from app.db import create_db_engine, create_session_factory
from app.errors import install_error_handling
from app.pagination import CursorCodec
from app.routers.items import router as items_router


class HealthResponse(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail closed: missing or unsafe configuration stops startup (spec section 5.3,
    # ADR 0007). Creating the engine does not connect, so /health stays a liveness
    # check and does not depend on the database being up.
    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.state.cursor_codec = CursorCodec(settings.cursor_key)
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(
    title="REST API Lab",
    description="Phase 1: Saved Content REST API",
    version="0.1.0",
    lifespan=lifespan,
)
install_error_handling(app)
app.include_router(items_router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")
