from fastapi import FastAPI
from pydantic import BaseModel

from app.errors import install_error_handling


class HealthResponse(BaseModel):
    status: str


app = FastAPI(
    title="REST API Lab",
    description="Phase 1: Saved Content REST API",
    version="0.1.0",
)
install_error_handling(app)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")
