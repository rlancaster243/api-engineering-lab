"""Uniform error contract.

Every error response uses one envelope, whether it was raised by application code,
by request validation, or by the framework:

    {"error": {"code": ..., "message": ..., "request_id": ...}}

FastAPI's defaults return ``{"detail": ...}`` and echo the submitted input, so the
handlers installed here replace them. A request id is generated server-side for every
request and echoed in ``X-Request-ID`` so a client report can be matched to a log line.
"""

import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class ErrorCode(StrEnum):
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    UNAUTHENTICATED = "UNAUTHENTICATED"
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
    ErrorCode.ASSET_NOT_FOUND: ErrorSpec(404, "Asset not found."),
    ErrorCode.UNAUTHENTICATED: ErrorSpec(401, "A valid X-API-Key header is required."),
    ErrorCode.VALIDATION_ERROR: ErrorSpec(422, "The request is invalid."),
    ErrorCode.ROUTE_NOT_FOUND: ErrorSpec(404, "No such route."),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(405, "Method not allowed for this route."),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(503, "The service is temporarily unavailable."),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, "An unexpected error occurred."),
}


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    request_id: str


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
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message or ERROR_SPECS[code].message,
            request_id=request_id,
        )
    )
    response_headers = {REQUEST_ID_HEADER: request_id}
    if ERROR_SPECS[code].status == 401:
        response_headers["WWW-Authenticate"] = "ApiKey"
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=ERROR_SPECS[code].status,
        content=envelope.model_dump(mode="json"),
        headers=response_headers,
    )


def validation_message(exc: RequestValidationError) -> str:
    """Name the offending fields without echoing the submitted values."""
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    return f"The request is invalid: {', '.join(fields)}" if fields else "The request is invalid."


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(request, exc.code, message=exc.message)


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(request, ErrorCode.VALIDATION_ERROR, message=validation_message(exc))


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    headers = dict(exc.headers) if exc.headers else None
    if exc.status_code == 404:
        return error_response(request, ErrorCode.ROUTE_NOT_FOUND, headers=headers)
    if exc.status_code == 405:
        return error_response(request, ErrorCode.METHOD_NOT_ALLOWED, headers=headers)
    logger.error("unmapped HTTP exception status=%s", exc.status_code)
    return error_response(request, ErrorCode.INTERNAL_ERROR, headers=headers)


BUSY_MARKERS = ("locked", "busy")


async def _handle_database_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, sqlite3.Error)
    # Only contention is transient. Any other storage error is a fault, and the text of a
    # storage error is the only diagnostic available without host access, so it is
    # returned to the caller rather than reduced to a fixed message.
    if isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc) for marker in BUSY_MARKERS
    ):
        logger.warning("database busy request_id=%s", _request_id(request), exc_info=exc)
        return error_response(request, ErrorCode.SERVICE_UNAVAILABLE)
    return _unexpected(request, exc)


def _unexpected(request: Request, exc: BaseException) -> JSONResponse:
    logger.error("unhandled exception request_id=%s", _request_id(request), exc_info=exc)
    return error_response(request, ErrorCode.INTERNAL_ERROR, message=str(exc) or None)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    return _unexpected(request, exc)


def install_error_handling(app: FastAPI) -> None:
    """Install the request-id middleware and the uniform error handlers on ``app``."""

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Always server-generated: a client-supplied id would be attacker-controlled
        # text in logs and responses.
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers.setdefault(REQUEST_ID_HEADER, request.state.request_id)
        return response

    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(sqlite3.Error, _handle_database_error)
    app.add_exception_handler(Exception, _handle_unexpected)
