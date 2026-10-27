"""Worker process — claims jobs, runs a handler, records the outcome.

Run as ``uv run python -m app.worker``. One job at a time; concurrency comes from
running several worker processes against the same ``DATABASE_URL``, which is what
``FOR UPDATE SKIP LOCKED`` is for (ADR 0010).

Each cycle sweeps expired leases first, so a crashed worker's job is either
reclaimed by a healthy worker or terminally failed once its attempts are gone.
A crashed worker never leaves a job stranded in ``RUNNING``.
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
from app.config import (
    DEFAULT_MAX_RETRY_BACKOFF_SECONDS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    ConfigError,
    load_settings,
    retry_delay_seconds,
)
from app.db import create_db_engine, create_session_factory
from app.logging import configure_logging, log_event
from app.models import Job

STOP_EVENTS = (signal.SIGTERM, signal.SIGINT)

TERMINAL_SUCCEEDED = "SUCCEEDED"
TERMINAL_FAILED = "FAILED"

#: Mirrors the SWEEP_SQL message so a logged event and the stored row agree.
LEASE_EXHAUSTED_MESSAGE = "lease expired after {attempt} attempt(s); no attempts remaining"


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
        retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
        max_retry_backoff_seconds: int = DEFAULT_MAX_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self.worker_id = worker_id or make_worker_id()
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_backoff_seconds = max_retry_backoff_seconds
        self.logger = logger or logging.getLogger("async_job_api")

    def run_once(self) -> bool:
        """Sweep, then claim and process at most one job; ``True`` when one was handled."""
        self._sweep()
        try:
            claim = self._claim()
        except SQLAlchemyError:
            self.logger.exception("claim failed; retrying on the next poll")
            return False
        if claim is None:
            return False
        job, kind = claim
        self._log_claimed(job, kind)
        self._process(job)
        return True

    def _sweep(self) -> None:
        """Terminally fail expired jobs with no attempts left.

        A sweep failure must not stop the worker: log it and claim anyway.
        """
        try:
            with self._session_factory() as session:
                swept = repository.sweep_expired(session)
                session.commit()
        except SQLAlchemyError:
            self.logger.exception("recovery sweep failed; continuing to claim")
            return
        for job_id, attempt in swept:
            log_event(
                self.logger,
                "job.failed",
                worker_id=self.worker_id,
                job_id=str(job_id),
                attempt=attempt,
                reason="lease_expired",
                last_error=LEASE_EXHAUSTED_MESSAGE.format(attempt=attempt),
            )

    def _claim(self) -> tuple[Job, repository.ClaimKind] | None:
        with self._session_factory() as session:
            claim = repository.claim_next_job(session, self.worker_id, self.lease_seconds)
            session.commit()
        return claim

    def _log_claimed(self, job: Job, kind: repository.ClaimKind) -> None:
        common = {
            "worker_id": self.worker_id,
            "job_id": str(job.id),
            "job_type": job.type,
            "attempt": job.attempt,
        }
        if kind == "reclaimed":
            log_event(self.logger, "job.recovered", **common)
        log_event(
            self.logger,
            "job.claimed",
            **common,
            lease_expires_at=job.lease_expires_at.isoformat()
            if job.lease_expires_at is not None
            else None,
        )

    def _process(self, job: Job) -> None:
        handler = handlers.resolve(job.type)
        if handler is None:
            self._fail(job, handlers.unknown_type_error(job.type))
            return
        try:
            result = handler(job.payload)
        except Exception as error:  # a handler failure must not kill the loop
            self.logger.exception("handler raised worker_id=%s job_id=%s", self.worker_id, job.id)
            self._fail(job, handlers.describe_failure(error))
            return
        self._finish(
            job,
            status=TERMINAL_SUCCEEDED,
            result=result,
            last_error=None,
            event="job.succeeded",
        )

    def _fail(self, job: Job, last_error: str) -> None:
        """Record a failed attempt: requeue with backoff, or fail terminally."""
        backoff = retry_delay_seconds(
            job.attempt,
            base=self.retry_backoff_seconds,
            ceiling=self.max_retry_backoff_seconds,
        )
        try:
            with self._session_factory() as session:
                outcome = repository.fail_attempt(
                    session,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    last_error=last_error,
                    backoff_seconds=backoff,
                )
                session.commit()
        except SQLAlchemyError:
            self.logger.exception(
                "failure recording failed worker_id=%s job_id=%s", self.worker_id, job.id
            )
            return

        if outcome is None:
            self._log_lease_lost(job)
            return

        status, attempt = outcome
        if status == "PENDING":
            log_event(
                self.logger,
                "job.retrying",
                worker_id=self.worker_id,
                job_id=str(job.id),
                job_type=job.type,
                attempt=attempt,
                max_attempts=job.max_attempts,
                backoff_seconds=backoff,
                last_error=last_error,
            )
            return

        log_event(
            self.logger,
            "job.failed",
            worker_id=self.worker_id,
            job_id=str(job.id),
            job_type=job.type,
            attempt=attempt,
            last_error=last_error,
        )

    def _log_lease_lost(self, job: Job) -> None:
        log_event(
            self.logger,
            "job.lease_lost",
            _level=logging.WARNING,
            worker_id=self.worker_id,
            job_id=str(job.id),
            attempt=job.attempt,
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
            self._log_lease_lost(job)
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
    retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
    max_retry_backoff_seconds: int = DEFAULT_MAX_RETRY_BACKOFF_SECONDS,
) -> bool:
    """One sweep-claim-run cycle on a fresh worker. The entry point tests drive."""
    worker = Worker(
        session_factory,
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
        worker_id=worker_id,
        logger=logger,
        retry_backoff_seconds=retry_backoff_seconds,
        max_retry_backoff_seconds=max_retry_backoff_seconds,
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
        retry_backoff_seconds=settings.retry_backoff_seconds,
        max_retry_backoff_seconds=settings.max_retry_backoff_seconds,
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
