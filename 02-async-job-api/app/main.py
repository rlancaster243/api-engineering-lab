from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import load_settings
from app.db import create_db_engine, create_session_factory
from app.errors import install_error_handling
from app.routers import jobs


class HealthResponse(BaseModel):
    status: str
    service: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(
    title="Async Job API Lab",
    description="Phase 2: async jobs and status APIs",
    version="0.1.0",
    lifespan=lifespan,
)

install_error_handling(app)
app.include_router(jobs.router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="02-async-job-api")
