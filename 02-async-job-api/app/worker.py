"""Worker process — claims PENDING jobs, runs a handler, records the outcome.

Run as ``uv run python -m app.worker``. One job at a time; concurrency comes from
running several worker processes against the same ``DATABASE_URL``, which is what
``FOR UPDATE SKIP LOCKED`` is for (ADR 0010).

A worker that dies mid-job leaves the row ``RUNNING`` with an expired lease and no
recovery — that gap is PR-2-03's, deliberately.
"""

import logging
import os
import signal
import socket
import sys
import threading
import uuid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app import handlers, repository
from app.config import ConfigError, load_settings
from app.db import create_db_engine, create_session_factory
from app.logging import configure_logging, log_event
from app.models import Job

STOP_EVENTS = (signal.SIGTERM, signal.SIGINT)

TERMINAL_SUCCEEDED = "SUCCEEDED"
TERMINAL_FAILED = "FAILED"


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int,
        poll_seconds: float,
        worker_id: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.worker_id = worker_id or make_worker_id()
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.logger = logger or logging.getLogger("async_job_api")

    def run_once(self) -> bool:
        """Claim and process at most one job; ``True`` when a job was handled."""
        try:
            job = self._claim()
        except SQLAlchemyError:
            self.logger.exception("claim failed; retrying on the next poll")
            return False
        if job is None:
            return False
        self._process(job)
        return True

    def _claim(self) -> Job | None:
        with self._session_factory() as session:
            job = repository.claim_next_job(session, self.worker_id, self.lease_seconds)
            session.commit()
        if job is not None:
            log_event(
                self.logger,
                "job.claimed",
                worker_id=self.worker_id,
                job_id=str(job.id),
                job_type=job.type,
                attempt=job.attempt,
                lease_expires_at=job.lease_expires_at.isoformat()
                if job.lease_expires_at is not None
                else None,
            )
        return job

    def _process(self, job: Job) -> None:
        handler = handlers.resolve(job.type)
        if handler is None:
            self._finish(
                job,
                status=TERMINAL_FAILED,
                result=None,
                last_error=handlers.unknown_type_error(job.type),
                event="job.failed",
            )
            return
        try:
            result = handler(job.payload)
        except Exception as error:  # a handler failure must not kill the loop
            self.logger.exception("handler raised worker_id=%s job_id=%s", self.worker_id, job.id)
            self._finish(
                job,
                status=TERMINAL_FAILED,
                result=None,
                last_error=handlers.describe_failure(error),
                event="job.failed",
            )
            return
        self._finish(
            job,
            status=TERMINAL_SUCCEEDED,
            result=result,
            last_error=None,
            event="job.succeeded",
        )

    def _finish(
        self,
        job: Job,
        *,
        status: str,
        result: dict | None,
        last_error: str | None,
        event: str,
    ) -> None:
        try:
            with self._session_factory() as session:
                rowcount = repository.complete_job(
                    session,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    status=status,
                    result=result,
                    last_error=last_error,
                )
                session.commit()
        except SQLAlchemyError:
            self.logger.exception(
                "completion failed worker_id=%s job_id=%s", self.worker_id, job.id
            )
            return

        if rowcount == 0:
            log_event(
                self.logger,
                "job.lease_lost",
                _level=logging.WARNING,
                worker_id=self.worker_id,
                job_id=str(job.id),
                attempt=job.attempt,
            )
            return

        log_event(
            self.logger,
            event,
            worker_id=self.worker_id,
            job_id=str(job.id),
            job_type=job.type,
            attempt=job.attempt,
            last_error=last_error,
        )

    def run_forever(self, stop: threading.Event) -> None:
        log_event(
            self.logger,
            "worker.started",
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            poll_seconds=self.poll_seconds,
        )
        try:
            while not stop.is_set():
                if not self.run_once() and not stop.is_set():
                    stop.wait(self.poll_seconds)
        finally:
            log_event(self.logger, "worker.stopped", worker_id=self.worker_id)


def run_once(
    session_factory: sessionmaker[Session],
    *,
    lease_seconds: int = 60,
    poll_seconds: float = 1.0,
    worker_id: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """One claim-and-run cycle on a fresh worker. The entry point tests drive."""
    worker = Worker(
        session_factory,
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
        worker_id=worker_id,
        logger=logger,
    )
    return worker.run_once()


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    logger = configure_logging()
    engine = create_db_engine(settings.database_url)
    worker = Worker(
        create_session_factory(engine),
        lease_seconds=settings.lease_seconds,
        poll_seconds=settings.worker_poll_seconds,
        logger=logger,
    )

    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("signal %s received; finishing the job in hand", signum)
        stop.set()

    for signal_number in STOP_EVENTS:
        signal.signal(signal_number, request_stop)

    try:
        worker.run_forever(stop)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
