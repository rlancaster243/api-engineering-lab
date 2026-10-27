"""Uniform error contract for the Async Job API (Project 2).

Every error response uses one envelope:

    {"error": {"code": ..., "message": ..., "request_id": ..., "details": [...]}}

Ported from 01-rest-api/app/errors.py; auth codes removed; Project 2 codes added.
Validation details are stripped to {field, rule} so submitted values are never echoed.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db_errors import is_service_unavailable

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class ErrorCode(StrEnum):
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    message: str


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.JOB_NOT_FOUND: ErrorSpec(404, "Job not found."),
    ErrorCode.VALIDATION_ERROR: ErrorSpec(422, "The request is invalid."),
    ErrorCode.ROUTE_NOT_FOUND: ErrorSpec(404, "No such route."),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(405, "Method not allowed for this route."),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(503, "The service is temporarily unavailable."),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, "An unexpected error occurred."),
}


class ErrorDetail(BaseModel):
    """Location and rule of a validation failure. Never carries submitted values."""

    field: str
    rule: str


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    request_id: str
    details: list[ErrorDetail] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    """Raise from application code to produce a contract-conformant error response."""

    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        self.code = code
        self.message = message or ERROR_SPECS[code].message
        super().__init__(self.message)

    @property
    def status_code(self) -> int:
        return ERROR_SPECS[self.code].status


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else str(uuid.uuid4())


def error_response(
    request: Request,
    code: ErrorCode,
    *,
    message: str | None = None,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message or ERROR_SPECS[code].message,
            request_id=request_id,
            details=details,
        )
    )
    response_headers = {REQUEST_ID_HEADER: request_id}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=ERROR_SPECS[code].status,
        content=envelope.model_dump(mode="json", exclude_none=True),
        headers=response_headers,
    )


def _validation_details(exc: RequestValidationError) -> list[ErrorDetail]:
    return [
        ErrorDetail(field=".".join(str(part) for part in error["loc"]), rule=str(error["type"]))
        for error in exc.errors()
    ]


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(request, exc.code, message=exc.message)


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(request, ErrorCode.VALIDATION_ERROR, details=_validation_details(exc))


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    headers = dict(exc.headers) if exc.headers else None
    if exc.status_code == 404:
        return error_response(request, ErrorCode.ROUTE_NOT_FOUND, headers=headers)
    if exc.status_code == 405:
        return error_response(request, ErrorCode.METHOD_NOT_ALLOWED, headers=headers)
    logger.error("unmapped HTTP exception status=%s", exc.status_code)
    return error_response(request, ErrorCode.INTERNAL_ERROR, headers=headers)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id(request)
    logger.error("unhandled exception request_id=%s", request_id, exc_info=exc)
    return error_response(request, ErrorCode.INTERNAL_ERROR)


async def _handle_dbapi_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DBAPIError)
    request_id = _request_id(request)
    if isinstance(exc, IntegrityError):
        logger.error("database integrity error request_id=%s", request_id, exc_info=exc)
        return error_response(request, ErrorCode.INTERNAL_ERROR)
    if is_service_unavailable(exc):
        logger.warning("database unavailable request_id=%s", request_id, exc_info=exc)
        return error_response(request, ErrorCode.SERVICE_UNAVAILABLE)
    logger.error("database error request_id=%s", request_id, exc_info=exc)
    return error_response(request, ErrorCode.INTERNAL_ERROR)


def install_error_handling(app: FastAPI) -> None:
    """Install the request-id middleware and the uniform error handlers on ``app``."""

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers.setdefault(REQUEST_ID_HEADER, request.state.request_id)
        return response

    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(DBAPIError, _handle_dbapi_error)
    app.add_exception_handler(Exception, _handle_unexpected)
