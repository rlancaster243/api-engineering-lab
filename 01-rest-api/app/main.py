from fastapi import FastAPI
from pydantic import BaseModel

from app.routers.items import router as items_router


class HealthResponse(BaseModel):
    status: str


app = FastAPI(
    title="REST API Lab",
    description="Phase 1: Saved Content REST API",
    version="0.1.0",
)
app.include_router(items_router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")
