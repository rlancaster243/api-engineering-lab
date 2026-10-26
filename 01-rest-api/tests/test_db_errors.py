"""Unit tests for database error classification."""

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.db_errors import is_service_unavailable


def test_operational_error_is_unavailable() -> None:
    assert is_service_unavailable(OperationalError("q", {}, Exception("down")))


def test_integrity_error_is_not_unavailable() -> None:
    assert not is_service_unavailable(IntegrityError("q", {}, Exception("dup")))


def test_connection_invalidated_dbapi_is_unavailable() -> None:
    exc = DBAPIError("q", {}, Exception("lost"))
    exc.connection_invalidated = True
    assert is_service_unavailable(exc)
