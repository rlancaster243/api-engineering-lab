"""Caller principal resolution (spec section 5).

Project 1A has one principal, the seeded development owner, and only in
``AUTH_MODE=local_dev``. In ``external_jwt`` mode (the default) every item request is
rejected until token verification lands in PR-05: there is no path from a failed or
absent credential to the development owner.
"""

import uuid

from fastapi import Request

from app.config import AuthMode, Settings
from app.errors import ApiError, ErrorCode
from app.models import DEV_USER_ID


def current_user_id(request: Request) -> uuid.UUID:
    settings: Settings = request.app.state.settings
    if settings.auth_mode is AuthMode.LOCAL_DEV:
        return DEV_USER_ID
    raise ApiError(ErrorCode.UNAUTHENTICATED)
