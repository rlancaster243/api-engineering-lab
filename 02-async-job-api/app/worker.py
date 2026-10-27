"""Worker process — claims jobs, runs a handler, records the outcome.

Run as ``uv run python -m app.worker``. One job at a time; concurrency comes from
running several worker processes against the same ``DATABASE_URL``, which is what
``FOR UPDATE SKIP LOCKED`` is for (ADR 0010).

Each cycle sweeps expired leases, reconciles stranded effects, then claims
reclaim-first: an expired ``RUNNING`` job with attempts left is taken over before
any ``PENDING`` work, so a crashed worker's job is never starved behind a busy
queue and never left stranded in ``RUNNING``. A failed attempt with budget left is
requeued with exponential backoff. Attempts are consumed at claim time (ADR 0010),
so a crash is charged against the job's budget.

The reconcile step closes falsification case 4 from the other side. A handler claims
its side effect in ``job_effects`` before performing it, so a reclaimed job re-runs
the handler but skips the effect. If the process died between the claim and the
confirmation, the effect stays ``INTENT``; once it is older than
``EFFECT_RECONCILE_GRACE_SECONDS`` — long enough that its handler cannot still be in
flight — a worker applies and confirms it.

A single cycle cannot take the process down: a reconcile that raises, a sweep that
raises, a claim that raises, a handler that raises or returns a non-object, and a
result the database refuses are each logged and the loop polls again.
``BaseException`` is deliberately not caught, so SIGINT, SIGTERM and a stop
propagated as ``SystemExit`` still end the process.

No failure on this path logs a traceback or any handler-authored message. A
formatted traceback repeats the exception message and source locals, and a claim
failure can surface a row value (a decode error quoting a payload field). Every
failure is logged as its class alone; the message itself is stored in
``jobs.last_error`` and served from there.
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
    DEFAULT_EFFECT_RECONCILE_GRACE_SECONDS,
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

#: Mirrors the SWEEP_SQL message, so the logged length and the stored row agree.
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
        effect_reconcile_grace_seconds: int = DEFAULT_EFFECT_RECONCILE_GRACE_SECONDS,
        apply_effect: handlers.EffectApplier = handlers.simulate_effect,
    ) -> None:
        self._session_factory = session_factory
        self.worker_id = worker_id or make_worker_id()
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_backoff_seconds = max_retry_backoff_seconds
        self.effect_reconcile_grace_seconds = effect_reconcile_grace_seconds
        self._apply_effect = apply_effect
        self.logger = logger or logging.getLogger("async_job_api")

    def run_once(self) -> bool:
        """Sweep, reconcile stranded effects, claim reclaim-first, process one job.

        ``True`` means one job was handled — including a job that was requeued
        rather than finished. Any exception from one cycle is logged as its class
        alone and swallowed so the loop keeps polling.

        The sweep and the reconcile sit inside the guard on purpose. Both contain
        their own error handling and continue to the claim, so the common database
        failure costs nothing here; the outer catch is only the backstop for an
        unexpected error, and an unguarded step would be the one remaining way for a
        cycle to end ``run_forever``.
        """
        try:
            self._sweep()
            self._reconcile_effects()
            claim = self._claim()
            if claim is None:
                return False
            job, kind = claim
            self._log_claimed(job, kind)
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

    def _reconcile_effects(self) -> None:
        """Apply stranded ``INTENT`` effects older than the grace period.

        A handler that dies between claiming an effect and confirming it leaves the
        row ``INTENT`` forever; without this step the effect is never applied and the
        outbox silently loses work. The grace period keeps the reconciler off a live
        handler's row: a young ``INTENT`` may still be applied by the worker that
        holds it, and applying it here would duplicate.

        The row is locked for the whole apply-and-confirm, in one transaction. Every
        worker reconciles on every cycle, so two of them will race for the same
        stranded row; the lock is what stops both from applying it. The loser blocks,
        re-reads a committed ``APPLIED`` row and moves on.

        An apply failure rolls back, leaving the row ``INTENT`` for a later cycle, and
        must not stop the worker: it logs the class and carries on to the claim.
        Neither the effect ``detail`` nor an exception message is logged — both are
        handler-authored, and ``app.logging``'s contract forbids them on a log line.
        """
        try:
            with self._session_factory() as session:
                pending = repository.pending_effects(
                    session, grace_seconds=self.effect_reconcile_grace_seconds
                )
        except Exception as error:
            self._log_write_error("effect reconcile read", None, error)
            return

        for job_id, effect_key, _detail in pending:
            try:
                with self._session_factory() as session:
                    locked = repository.lock_pending_effect(
                        session, job_id=job_id, effect_key=effect_key
                    )
                    if locked is None:
                        # Confirmed by another reconciler, or no longer old enough.
                        continue
                    age_seconds, detail = locked
                    self._apply_effect(job_id, effect_key, detail)
                    transitioned = repository.mark_effect_applied(
                        session, job_id=job_id, effect_key=effect_key
                    )
                    session.commit()
            except Exception as error:
                self._log_write_error("effect reconcile apply", None, error)
                continue
            if not transitioned:
                continue
            log_event(
                self.logger,
                "effect.reconciled",
                worker_id=self.worker_id,
                job_id=str(job_id),
                effect_key=effect_key,
                age_seconds=age_seconds,
            )

    def _sweep(self) -> None:
        """Terminally fail expired jobs with no attempts left.

        A sweep failure must not stop the worker: log the class and claim anyway.
        """
        try:
            with self._session_factory() as session:
                swept = repository.sweep_expired(session)
                session.commit()
        except SQLAlchemyError as error:
            self._log_write_error("recovery sweep", None, error)
            return
        for job_id, attempt in swept:
            last_error = LEASE_EXHAUSTED_MESSAGE.format(attempt=attempt)
            log_event(
                self.logger,
                "job.failed",
                worker_id=self.worker_id,
                job_id=str(job_id),
                attempt=attempt,
                reason="lease_expired",
                error_class="LeaseExpired",
                last_error_chars=len(last_error),
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
            self._fail(job, handlers.unknown_type_failure(job.type))
            return
        context = handlers.HandlerContext(
            session_factory=self._session_factory,
            job_id=job.id,
            apply_effect=self._apply_effect,
        )
        try:
            result = handler(job.payload, context)
        except Exception as error:  # a handler failure must not kill the loop
            self._fail(job, handlers.describe_failure(error))
            return
        if not isinstance(result, dict):
            self._fail(job, handlers.invalid_result_failure(result))
            return
        self._finish(
            job,
            status=TERMINAL_SUCCEEDED,
            result=result,
            failure=None,
            event="job.succeeded",
        )

    def _fail(self, job: Job, failure: handlers.FailureDetail) -> None:
        """Record a failed attempt: requeue with backoff, or fail terminally.

        ``failure.last_error`` is bound as a statement parameter and nothing else:
        it is handler-authored text that may quote client data, so the events below
        carry ``error_class`` and ``last_error_chars`` and never the message.

        If the row cannot be written, the job stays ``RUNNING`` on an expired lease
        — the same state a crashed worker leaves, which the next cycle's sweep
        recovers. Retrying the write here would need the lease, which may be gone.
        """
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
                    last_error=failure.last_error,
                    backoff_seconds=backoff,
                )
                session.commit()
        except Exception as error:
            self._log_write_error("failure recording", job, error)
            return

        if outcome is None:
            self._log_lease_lost(job)
            return

        status, attempt = outcome
        fields: dict[str, object] = {
            "worker_id": self.worker_id,
            "job_id": str(job.id),
            "job_type": job.type,
            "attempt": attempt,
            "error_class": failure.error_class,
            "last_error_chars": len(failure.last_error),
        }
        if status == "PENDING":
            log_event(
                self.logger,
                "job.retrying",
                **fields,
                max_attempts=job.max_attempts,
                backoff_seconds=backoff,
            )
            return

        log_event(self.logger, "job.failed", **fields)

    def _log_lease_lost(self, job: Job) -> None:
        log_event(
            self.logger,
            "job.lease_lost",
            _level=logging.WARNING,
            worker_id=self.worker_id,
            job_id=str(job.id),
            attempt=job.attempt,
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
            self._log_lease_lost(job)
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

    def _log_write_error(self, action: str, job: Job | None, error: Exception) -> None:
        """Log the class only, naming which write failed.

        A formatted traceback is unsafe on every one of these paths: SQLAlchemy
        repeats the statement and its bound parameters, which hold the handler's
        result and the ``last_error`` message on a completion or failure, and can
        hold a row value on a sweep.
        """
        self.logger.error(
            "%s failed worker_id=%s job_id=%s error_class=%s",
            action,
            self.worker_id,
            None if job is None else job.id,
            type(error).__name__,
        )

    def _log_completion_error(self, job: Job, error: Exception) -> None:
        """Log a completion failure's class only — see ``_log_write_error``."""
        self._log_write_error("completion", job, error)

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
    effect_reconcile_grace_seconds: int = DEFAULT_EFFECT_RECONCILE_GRACE_SECONDS,
    apply_effect: handlers.EffectApplier = handlers.simulate_effect,
) -> bool:
    """One sweep-reconcile-claim-run cycle on a fresh worker. The entry point tests drive."""
    worker = Worker(
        session_factory,
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
        worker_id=worker_id,
        logger=logger,
        retry_backoff_seconds=retry_backoff_seconds,
        max_retry_backoff_seconds=max_retry_backoff_seconds,
        effect_reconcile_grace_seconds=effect_reconcile_grace_seconds,
        apply_effect=apply_effect,
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
        effect_reconcile_grace_seconds=settings.effect_reconcile_grace_seconds,
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
