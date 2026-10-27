"""Worker process — claims PENDING jobs, runs a handler, records the outcome.

Run as ``uv run python -m app.worker``. One job at a time; concurrency comes from
running several worker processes against the same ``DATABASE_URL``, which is what
``FOR UPDATE SKIP LOCKED`` is for (ADR 0010).

A single cycle cannot take the process down: a claim that raises, a handler that
raises or returns a non-object, and a result the database refuses are each logged
and the loop polls again. There is no lease-expiry recovery in this PR — a worker
killed mid-job still leaves its row ``RUNNING`` with a lease that expires and is
never reclaimed; that sweep is #17. Attempts are consumed at claim time (ADR 0010).
"""

import logging
import os
import signal
import socket
import sys
import threading
import uuid

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
        """Claim and process at most one job; ``True`` when a job was handled.

        Any exception from one cycle is logged and swallowed so the loop keeps
        polling: ``BaseException`` is deliberately not caught, so SIGINT, SIGTERM and
        a stop propagated as ``SystemExit`` still end the process.

        The failure is logged as its class alone, with no traceback. A formatted
        traceback repeats the exception message and source locals, and a claim
        failure can surface a row value (a decode error quoting a payload field).
        """
        try:
            job = self._claim()
            if job is None:
                return False
            self._process(job)
        except Exception as error:
            log_event(
                self.logger,
                "worker.cycle_failed",
                _level=logging.ERROR,
                worker_id=self.worker_id,
                error_class=type(error).__name__,
            )
            return False
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
                failure=handlers.unknown_type_failure(job.type),
                event="job.failed",
            )
            return
        try:
            result = handler(job.payload)
        except Exception as error:  # a handler failure must not kill the loop
            self._finish(
                job,
                status=TERMINAL_FAILED,
                result=None,
                failure=handlers.describe_failure(error),
                event="job.failed",
            )
            return
        if not isinstance(result, dict):
            self._finish(
                job,
                status=TERMINAL_FAILED,
                result=None,
                failure=handlers.invalid_result_failure(result),
                event="job.failed",
            )
            return
        self._finish(
            job,
            status=TERMINAL_SUCCEEDED,
            result=result,
            failure=None,
            event="job.succeeded",
        )

    def _complete(
        self,
        job: Job,
        *,
        status: str,
        result: dict | None,
        last_error: str | None,
    ) -> int:
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
        return rowcount

    def _finish(
        self,
        job: Job,
        *,
        status: str,
        result: dict | None,
        failure: handlers.FailureDetail | None,
        event: str,
    ) -> None:
        try:
            rowcount = self._complete(
                job,
                status=status,
                result=result,
                last_error=None if failure is None else failure.last_error,
            )
        except Exception as error:
            self._log_completion_error(job, error)
            if status == TERMINAL_SUCCEEDED:
                self._finish(
                    job,
                    status=TERMINAL_FAILED,
                    result=None,
                    failure=handlers.completion_failure(error),
                    event="job.failed",
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

        fields: dict[str, object] = {
            "worker_id": self.worker_id,
            "job_id": str(job.id),
            "job_type": job.type,
            "attempt": job.attempt,
        }
        if failure is not None:
            fields["error_class"] = failure.error_class
            fields["last_error_chars"] = len(failure.last_error)
        log_event(self.logger, event, **fields)

    def _log_completion_error(self, job: Job, error: Exception) -> None:
        """Log the class only.

        SQLAlchemy's formatted traceback repeats the statement and its bound
        parameters, and for a completion those hold the handler's result and the
        ``last_error`` message — neither may reach a log line.
        """
        self.logger.error(
            "completion failed worker_id=%s job_id=%s error_class=%s",
            self.worker_id,
            job.id,
            type(error).__name__,
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
