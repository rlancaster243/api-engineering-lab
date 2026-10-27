"""Data access layer — all SQL lives here."""

import uuid
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import Job

# The claim is one statement: the sub-select locks the candidate row and the
# UPDATE moves it to RUNNING in the same transaction, so there is no
# read-then-write gap for a second worker to slip through.
#
# ORDER BY created_at, id gives FIFO with a deterministic tie-break.
# attempt < max_attempts keeps exhausted jobs out of the claim path; they belong
# to PR-2-03's terminal-failure handling.
# attempt increments here, at claim time, so a worker that dies mid-job has
# still consumed an attempt (see ADR 0010 Consequences).
CLAIM_SQL = text(
    """
    UPDATE jobs
    SET status = 'RUNNING',
        attempt = attempt + 1,
        lease_owner = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
        claimed_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'PENDING' AND attempt < max_attempts
        ORDER BY created_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *
    """
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


def insert_job(session: Session, job: Job) -> None:
    session.add(job)


def get_job_by_id(session: Session, job_id: uuid.UUID) -> Job | None:
    return session.get(Job, job_id)


def claim_next_job(session: Session, worker_id: str, lease_seconds: int) -> Job | None:
    """Claim the oldest claimable PENDING job for ``worker_id``; ``None`` if the queue is empty."""
    row = session.execute(
        CLAIM_SQL, {"worker_id": worker_id, "lease_seconds": lease_seconds}
    ).one_or_none()
    if row is None:
        return None
    return Job(**row._mapping)


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
