from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel

from app.config import AuthMode, load_settings
from app.db import create_db_engine, create_session_factory
from app.errors import install_error_handling
from app.pagination import CursorCodec
from app.ready import database_is_ready
from app.routers.items import router as items_router
from app.tokens import JwksKeySource, TokenVerifier


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
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
    # Keys are fetched lazily on the first token, never at startup, so a provider outage
    # cannot stop the service from booting (and /health stays a liveness check).
    if settings.auth_mode is AuthMode.EXTERNAL_JWT and settings.oidc is not None:
        app.state.token_verifier = TokenVerifier(
            settings.oidc,
            JwksKeySource(settings.oidc.jwks_url, settings.oidc.jwks_cache_seconds),
        )
    else:
        app.state.token_verifier = None
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


@app.get("/ready", response_model=ReadyResponse)
def ready(request: Request) -> ReadyResponse:
    session = request.app.state.session_factory()
    try:
        database_is_ready(session)
    finally:
        session.close()
    return ReadyResponse(status="ready")
