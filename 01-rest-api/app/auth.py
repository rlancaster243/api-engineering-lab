"""Caller principal resolution (spec section 5).

Two steps, landed in two PRs so behavior stays fail-closed in between:

- PR-05 (this module's ``current_principal``): verify the bearer access token and return
  the verified ``Principal``. Nothing maps a principal to a stored user yet.
- PR-06: ``current_user_id`` resolves the verified principal to an internal user row via
  ``UserRepository.resolve``. There is no path from a failed or absent credential to the
  development owner in ``external_jwt`` mode.

The seeded development owner exists only in ``AUTH_MODE=local_dev``.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import AuthMode, Settings
from app.db import get_session
from app.errors import ApiError, ErrorCode
from app.models import DEV_USER_ID
from app.tokens import InsufficientScope, InvalidToken, KeysUnavailable, Principal, TokenVerifier
from app.users import UserRepository


def _bearer_token(request: Request) -> str:
    """Extract the token from ``Authorization: Bearer <token>`` or refuse with 401."""
    header = request.headers.get("Authorization")
    if header is None:
        raise ApiError(ErrorCode.UNAUTHENTICATED)
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise ApiError(ErrorCode.UNAUTHENTICATED)
    return parts[1]


def current_principal(request: Request) -> Principal:
    token = _bearer_token(request)
    verifier: TokenVerifier | None = request.app.state.token_verifier
    if verifier is None:
        # local_dev: there is no issuer to verify against.
        raise ApiError(ErrorCode.UNAUTHENTICATED)
    try:
        return verifier.verify(token)
    except InvalidToken:
        raise ApiError(ErrorCode.INVALID_TOKEN) from None
    except KeysUnavailable:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE) from None
    except InsufficientScope:
        raise ApiError(ErrorCode.INSUFFICIENT_SCOPE) from None


def current_user_id(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> uuid.UUID:
    settings: Settings = request.app.state.settings
    if settings.auth_mode is AuthMode.LOCAL_DEV:
        return DEV_USER_ID
    principal = current_principal(request)
    return UserRepository(session).resolve(principal.issuer, principal.subject)
