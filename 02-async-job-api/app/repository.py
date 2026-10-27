"""Data access layer — all SQL lives here."""

import uuid
from typing import Any, Literal

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import Job

ClaimKind = Literal["reclaimed", "pending"]

# The claim is two probes rather than one OR: a single predicate covering both
# PENDING and expired-RUNNING defeats both partial indexes and falls back to a
# sequential scan with a spill-to-disk sort (measured at 200k rows). Each probe
# below uses the index built for it.
#
# Reclaim runs first so a crashed worker's job is never starved behind a busy
# PENDING queue.
#
# ORDER BY created_at, id gives FIFO with a deterministic tie-break.
# attempt increments here, at claim time, so a worker that dies mid-job has
# still consumed an attempt (see ADR 0010 Consequences).
_CLAIM_UPDATE = """
    UPDATE jobs
    SET status = 'RUNNING',
        attempt = attempt + 1,
        lease_owner = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
        claimed_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE {predicate}
        ORDER BY created_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *
"""

RECLAIM_SQL = text(
    _CLAIM_UPDATE.format(
        predicate="status = 'RUNNING' AND lease_expires_at < now() AND attempt < max_attempts"
    )
)

PENDING_SQL = text(
    _CLAIM_UPDATE.format(
        predicate="status = 'PENDING' AND attempt < max_attempts AND next_attempt_at <= now()"
    )
)

# Owner-scoped by lease so a worker whose lease was taken over cannot overwrite
# the new owner's result. Clearing the lease keeps ck_jobs_lease_pair satisfied
# and stops a finished row looking in-flight.
COMPLETE_SQL = text(
    """
    UPDATE jobs
    SET status = :status,
        result = :result,
        last_error = :last_error,
        finished_at = now(),
        lease_owner = NULL,
        lease_expires_at = NULL
    WHERE id = :job_id AND lease_owner = :worker_id AND status = 'RUNNING'
    RETURNING id
    """
).bindparams(bindparam("result", type_=JSONB(none_as_null=True)))

# Expired jobs with no attempts left have no future in the claim path: the
# `attempt < max_attempts` guard refuses them, so without this they would sit
# RUNNING forever. Terminally failing them here is what keeps the claim's guard
# from becoming a dead end.
SWEEP_SQL = text(
    """
    UPDATE jobs
    SET status = 'FAILED',
        last_error = 'lease expired after ' || attempt || ' attempt(s); no attempts remaining',
        finished_at = now(),
        lease_owner = NULL,
        lease_expires_at = NULL
    WHERE status = 'RUNNING' AND lease_expires_at < now() AND attempt >= max_attempts
    RETURNING id, attempt
    """
)

# One statement on purpose: "requeue while budget remains" and "fail terminally
# when exhausted" are two outcomes of the same event. Split into two statements, a
# zero-row result could not distinguish "lease lost" from "budget exhausted".
# RETURNING status makes the outcome explicit.
FAIL_SQL = text(
    """
    UPDATE jobs
    SET status = CASE WHEN attempt < max_attempts THEN 'PENDING' ELSE 'FAILED' END,
        last_error = :last_error,
        finished_at = CASE WHEN attempt < max_attempts THEN NULL ELSE now() END,
        next_attempt_at = CASE WHEN attempt < max_attempts
                               THEN now() + make_interval(secs => :backoff_seconds)
                               ELSE next_attempt_at END,
        lease_owner = NULL,
        lease_expires_at = NULL
    WHERE id = :job_id AND lease_owner = :worker_id AND status = 'RUNNING'
    RETURNING status, attempt, next_attempt_at
    """
)


def insert_job(session: Session, job: Job) -> None:
    session.add(job)


def get_job_by_id(session: Session, job_id: uuid.UUID) -> Job | None:
    return session.get(Job, job_id)


def claim_next_job(
    session: Session, worker_id: str, lease_seconds: int
) -> tuple[Job, ClaimKind] | None:
    """Claim the oldest claimable job for ``worker_id``; ``None`` if nothing is claimable.

    Tries the reclaim probe first (a job whose lease expired), then the pending
    probe. Both run in the caller's transaction.
    """
    params = {"worker_id": worker_id, "lease_seconds": lease_seconds}
    for kind, statement in (("reclaimed", RECLAIM_SQL), ("pending", PENDING_SQL)):
        row = session.execute(statement, params).one_or_none()
        if row is not None:
            return Job(**row._mapping), kind  # type: ignore[arg-type]
    return None


def sweep_expired(session: Session) -> list[tuple[uuid.UUID, int]]:
    """Fail expired jobs that have no attempts left. Returns ``(job_id, attempt)`` pairs.

    Idempotent and safe to run from every worker concurrently.
    """
    rows = session.execute(SWEEP_SQL).all()
    return [(row.id, row.attempt) for row in rows]


def complete_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    status: str,
    result: dict[str, Any] | None = None,
    last_error: str | None = None,
) -> int:
    """Record a terminal status for a job this worker still owns.

    Returns the number of rows updated: zero means the lease was lost, and the
    caller must log rather than retry.
    """
    result_proxy = session.execute(
        COMPLETE_SQL,
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "status": status,
            "result": result,
            "last_error": last_error,
        },
    )
    return result_proxy.rowcount


def fail_attempt(
    session: Session,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    last_error: str,
    backoff_seconds: int,
) -> tuple[str, int] | None:
    """Requeue or terminally fail a failed attempt, in one owner-scoped statement.

    Returns ``(status, attempt)`` where status is ``PENDING`` (requeued) or
    ``FAILED`` (attempts exhausted), or ``None`` when the lease was lost.
    """
    row = session.execute(
        FAIL_SQL,
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "last_error": last_error,
            "backoff_seconds": backoff_seconds,
        },
    ).one_or_none()
    if row is None:
        return None
    return row.status, row.attempt
