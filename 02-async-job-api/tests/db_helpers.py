"""Shared database helpers for the PostgreSQL-backed tests.

Concurrency helpers live here so ``test_claims.py`` and ``test_worker.py`` use the
same machinery, and so a claim race is always run the same way.
"""

import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, NullPool, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app import repository
from app.db import create_session_factory


def new_session_factory(database_url: str) -> sessionmaker[Session]:
    """A factory whose every checkout is a fresh physical connection.

    ``NullPool`` matters: a pooled engine could hand two threads the same
    connection, and two "concurrent" claimers sharing one connection would not
    falsify anything about locking.
    """
    engine = create_engine(database_url, poolclass=NullPool)
    return create_session_factory(engine)


def dispose(factory: sessionmaker[Session]) -> None:
    factory.kw["bind"].dispose()


def clear_jobs(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM jobs"))
        conn.commit()


def seed_job(
    engine: Engine,
    *,
    job_type: str = "echo",
    payload: dict[str, Any] | None = None,
    status: str = "PENDING",
    attempt: int = 0,
    max_attempts: int = 3,
    created_at: datetime | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs "
                "(id, type, payload, status, attempt, max_attempts, created_at, "
                " lease_owner, lease_expires_at) "
                "VALUES (:id, :type, CAST(:payload AS jsonb), :status, :attempt, :max_attempts, "
                " COALESCE(CAST(:created_at AS timestamptz), now()), "
                " :lease_owner, CAST(:lease_expires_at AS timestamptz))"
            ),
            {
                "id": job_id,
                "type": job_type,
                "payload": json.dumps(payload if payload is not None else {}),
                "status": status,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "created_at": created_at,
                "lease_owner": lease_owner,
                "lease_expires_at": lease_expires_at,
            },
        )
        conn.commit()
    return job_id


def read_job(engine: Engine, job_id: uuid.UUID) -> Any:
    with engine.connect() as conn:
        return conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).one()


def claim_once(database_url: str, worker_id: str, lease_seconds: int = 60) -> uuid.UUID | None:
    """Claim once on a fresh connection owned by the calling thread."""
    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            job = repository.claim_next_job(session, worker_id, lease_seconds)
            session.commit()
            return None if job is None else job.id
    finally:
        dispose(factory)


def run_concurrently(*calls: Callable[[], Any]) -> list[BaseException | None]:
    """Release every call together on its own thread and join.

    Returns one slot per call: the exception it raised, or ``None``. Exceptions are
    captured rather than swallowed so a failure inside a thread still fails the test.
    """
    barrier = threading.Barrier(len(calls))
    outcomes: list[BaseException | None] = [None] * len(calls)

    def _wrap(index: int, call: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=10)
            call()
        except BaseException as error:  # noqa: BLE001 - re-raised by the caller's assertion
            outcomes[index] = error

    threads = [
        threading.Thread(target=_wrap, args=(index, call), daemon=True)
        for index, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), f"thread did not finish: {thread!r}"
    return outcomes


class ClaimRecorder:
    """Collects one claim result per worker from concurrent threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.claimed: dict[str, uuid.UUID | None] = {}

    def claim(self, database_url: str, worker_id: str, lease_seconds: int = 60) -> None:
        job_id = claim_once(database_url, worker_id, lease_seconds)
        with self._lock:
            self.claimed[worker_id] = job_id

    def call(
        self, database_url: str, worker_id: str, lease_seconds: int = 60
    ) -> Callable[[], None]:
        def _run() -> None:
            self.claim(database_url, worker_id, lease_seconds)

        return _run
