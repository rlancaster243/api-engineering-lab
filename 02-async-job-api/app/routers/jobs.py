"""Job submission and status endpoints."""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import service
from app.db import get_session
from app.errors import ApiError, ErrorCode

router = APIRouter(prefix="/v1/jobs")

DbSession = Annotated[Session, Depends(get_session)]

_MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KiB
_IDEMPOTENCY_KEY_MAX_LENGTH = 255

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


def _idempotency_key(raw: str | None) -> str | None:
    """Normalise the header before it is stored or hashed.

    Trimming first is what makes ``" abc "`` and ``"abc"`` one key rather than two
    jobs that look identical to the client, and it keeps the stored value the one
    the fingerprint was computed over. An all-whitespace header is treated as
    absent: it carries no usable identity, and rejecting it would fail a request
    that is otherwise well formed for a reason the client cannot see.

    The length limit is enforced here, not by Pydantic, because Pydantic's own
    message can quote the offending value. ``ApiError`` renders the fixed envelope
    instead.
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ApiError(ErrorCode.VALIDATION_ERROR)
    return key


def _result_object(result: Any) -> dict | None:
    """The response contract is object-or-null; a legacy non-object row must not 500.

    The worker refuses to store a non-dict result, so this only covers rows written
    before that guard existed.
    """
    return result if isinstance(result, dict) else None


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
    idempotency_key: IdempotencyKeyHeader = None,
) -> SubmitJobResponse:
    outcome = service.submit_job(
        session,
        job_type=body.type,
        payload=body.payload,
        max_attempts=body.max_attempts,
        idempotency_key=_idempotency_key(idempotency_key),
    )
    job = outcome.job
    response.headers["Location"] = str(request.url_for("get_job", job_id=str(job.id)))
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
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
        result=_result_object(job.result),
        last_error=job.last_error,
    )
