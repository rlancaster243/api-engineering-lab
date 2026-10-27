"""Job submission and status endpoints."""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import service
from app.db import get_session
from app.errors import ApiError, ErrorCode

router = APIRouter(prefix="/v1/jobs")

DbSession = Annotated[Session, Depends(get_session)]

_MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KiB


class SubmitJobRequest(BaseModel):
    type: str = Field(..., pattern=r"^[a-z][a-z0-9_.\-]{0,63}$")
    payload: dict[str, Any]
    max_attempts: int = Field(default=3, ge=1, le=20)

    @field_validator("payload")
    @classmethod
    def payload_size(cls, v: dict) -> dict:
        raw = json.dumps(v, allow_nan=False)
        if len(raw.encode()) > _MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds 64 KiB limit")
        return v


class SubmitJobResponse(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime


class JobStatusResponse(BaseModel):
    id: uuid.UUID
    type: str
    status: str
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    result: dict | None = None
    last_error: str | None = None


@router.post("", status_code=202)
def submit_job(
    body: SubmitJobRequest,
    response: Response,
    request: Request,
    session: DbSession,
) -> SubmitJobResponse:
    job = service.submit_job(
        session,
        job_type=body.type,
        payload=body.payload,
        max_attempts=body.max_attempts,
    )
    response.headers["Location"] = str(request.url_for("get_job", job_id=str(job.id)))
    return SubmitJobResponse(
        id=job.id,
        status=job.status,
        created_at=job.created_at,
    )


@router.get("/{job_id}", name="get_job")
def get_job(
    job_id: uuid.UUID,
    session: DbSession,
) -> JobStatusResponse:
    job = service.get_job(session, job_id)
    if job is None:
        raise ApiError(ErrorCode.JOB_NOT_FOUND)
    return JobStatusResponse(
        id=job.id,
        type=job.type,
        status=job.status,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        result=job.result,
        last_error=job.last_error,
    )
