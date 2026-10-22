from collections.abc import Generator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_db_engine(database_url: str) -> Engine:
    # pool_pre_ping replaces connections the server has dropped instead of failing
    # the first request that draws one.
    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(request: Request) -> Generator[Session, None, None]:
    """One session per request. Closing rolls back anything left uncommitted."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
