"""Classify database errors for HTTP mapping."""

from sqlalchemy.exc import DBAPIError, OperationalError

_UNAVAILABLE_SQLSTATES = frozenset(
    {
        "08000",  # connection_exception
        "08003",  # connection_does_not_exist
        "08006",  # connection_failure
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
        "53300",  # too_many_connections
    }
)


def sqlstate(exc: BaseException) -> str | None:
    """The PostgreSQL SQLSTATE for a driver exception, when the driver exposes one."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    state = getattr(orig, "sqlstate", None)
    if state is not None:
        return state
    diag = getattr(orig, "diag", None)
    return getattr(diag, "sqlstate", None) if diag is not None else None


def is_service_unavailable(exc: BaseException) -> bool:
    """True when the failure is transient infrastructure, not a data invariant."""
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    state = sqlstate(exc)
    if state is not None and state in _UNAVAILABLE_SQLSTATES:
        return True
    orig = getattr(exc, "orig", None)
    if orig is not None and type(orig).__name__ in {"OperationalError", "InterfaceError"}:
        return True
    return False
