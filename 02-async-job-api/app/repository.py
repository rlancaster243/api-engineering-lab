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

# The outbox claim. ``ON CONFLICT DO NOTHING`` is the whole guarantee: the primary
# key on (job_id, effect_key) makes the first insert the only one that returns a
# row, so a handler re-run after a crash is told "already claimed" and must not
# apply the effect. RETURNING rather than a rowcount because a conflict is silent
# in rowcount terms only if the driver is; a returned row is unambiguous.
CLAIM_EFFECT_SQL = text("""
    INSERT INTO job_effects (job_id, effect_key, detail)
    VALUES (:job_id, :effect_key, :detail)
    ON CONFLICT (job_id, effect_key) DO NOTHING
    RETURNING job_id
""").bindparams(bindparam("detail", type_=JSONB(none_as_null=True)))

# The ``status = 'INTENT'`` guard makes confirmation idempotent: a second confirm
# returns nothing, so a reconciler that lost a race cannot report a second apply.
APPLY_EFFECT_SQL = text("""
    UPDATE job_effects SET status = 'APPLIED', applied_at = now()
    WHERE job_id = :job_id AND effect_key = :effect_key AND status = 'INTENT'
    RETURNING job_id
""")

# ``created_at < now() - grace`` is what keeps the reconciler off a live handler's
# in-flight row: a young INTENT may still be applied by the worker holding it.
PENDING_EFFECTS_SQL = text("""
    SELECT job_id, effect_key, detail FROM job_effects
    WHERE status = 'INTENT' AND created_at < now() - make_interval(secs => :grace_seconds)
    ORDER BY created_at
    LIMIT :limit
""")

# Every worker reconciles on every cycle, so two of them will race for the same
# stranded row as soon as there are two. ``FOR UPDATE`` — not ``SKIP LOCKED`` — is
# the fix: the loser blocks until the winner commits, then re-checks its predicate
# against the new row version, sees APPLIED and drops the row. The effect is applied
# once. ``SKIP LOCKED`` would also apply once here, but it would hide contention;
# blocking makes the second reconciler simply wait and then find nothing to do.
#
# The ``status`` predicate is repeated here on purpose. It is the mutable one: the
# other reconciler's commit is exactly what changes it, so re-checking it under the
# lock is what turns the loser's read into ``None``. The grace condition is not
# repeated — ``created_at`` is immutable, so a row that satisfied it in the unlocked
# read still satisfies it here.
LOCK_PENDING_EFFECT_SQL = text("""
    SELECT job_id, effect_key, detail,
           EXTRACT(EPOCH FROM (now() - created_at))::bigint AS age_seconds
    FROM job_effects
    WHERE job_id = :job_id AND effect_key = :effect_key AND status = 'INTENT'
    FOR UPDATE
""")


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


def claim_effect(
    session: Session,
    *,
    job_id: uuid.UUID,
    effect_key: str,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Claim ``(job_id, effect_key)``; ``True`` means this caller must apply it.

    ``False`` means the effect was already claimed — by an earlier attempt of the
    same job, or by a live attempt of a reclaimed job — so the caller must skip the
    external call. Committing is the caller's job: the claim must be durable before
    the effect is applied, which is why it is its own session and transaction.
    """
    row = session.execute(
        CLAIM_EFFECT_SQL,
        {"job_id": job_id, "effect_key": effect_key, "detail": detail},
    ).one_or_none()
    return row is not None


def mark_effect_applied(session: Session, *, job_id: uuid.UUID, effect_key: str) -> bool:
    """Flip a claimed effect to ``APPLIED``; ``True`` when a row transitioned."""
    row = session.execute(
        APPLY_EFFECT_SQL, {"job_id": job_id, "effect_key": effect_key}
    ).one_or_none()
    return row is not None


def pending_effects(
    session: Session, *, grace_seconds: int, limit: int = 100
) -> list[tuple[uuid.UUID, str, dict | None]]:
    """Stranded ``INTENT`` effects older than ``grace_seconds``, oldest first.

    The grace window is what keeps the reconciler off a live handler's row: an
    ``INTENT`` younger than the lease's likely in-flight window may still be applied
    by the worker holding it.
    """
    rows = session.execute(
        PENDING_EFFECTS_SQL, {"grace_seconds": grace_seconds, "limit": int(limit)}
    ).all()
    return [(row.job_id, row.effect_key, row.detail) for row in rows]


def lock_pending_effect(
    session: Session, *, job_id: uuid.UUID, effect_key: str
) -> tuple[int, dict | None] | None:
    """Lock a still-``INTENT`` effect row for this worker; ``None`` if it is not one.

    Returns ``(age_seconds, detail)`` for the caller that won the lock. The lock is
    held until the caller commits or rolls back, which is what makes the apply
    exclusive: a second reconciler blocks here, then re-evaluates the predicate
    against the committed row and gets ``None`` because the status is now
    ``APPLIED``.

    Only callers that already read the row through :func:`pending_effects` should
    call this — that is where the grace-period filter lives. Repeating an immutable
    ``created_at`` bound here would look like a second guard while guarding nothing.
    """
    row = session.execute(
        LOCK_PENDING_EFFECT_SQL, {"job_id": job_id, "effect_key": effect_key}
    ).one_or_none()
    if row is None:
        return None
    return int(row.age_seconds), row.detail
