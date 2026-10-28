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

# How long the renewal statement may wait for a row lock.
#
# ``Worker._process`` stops the heartbeat in a ``finally`` before any completion
# write, and ``_LeaseHeartbeat.stop`` joins that thread without a timeout: a renewal
# still in flight could otherwise commit after the completion and leave a finished
# row holding a live lease. An unbounded join is only safe if the statement it waits
# for is bounded, and by default it is not — a concurrent reclaim holds this row
# under ``SELECT ... FOR UPDATE`` for the length of its own transaction, so a plain
# renewal would block indefinitely and the join would hang the whole worker on a
# handler that has already returned. The wait is therefore bounded here, at the
# statement, rather than at the join.
#
# Exceeding it fails the renewal with SQLSTATE ``55P03`` (``lock_not_available``),
# which surfaces as ``OperationalError`` and is handled exactly like any other
# failed renewal: logged by class, counted as one missed renewal, and retried on the
# next tick while the lease still has two thirds of its life left.
RENEWAL_LOCK_TIMEOUT_MS = 500

# ``SET LOCAL`` takes a literal, not a bind parameter, so the value is interpolated
# from the integer constant above — never from anything a caller supplies.
SET_RENEWAL_LOCK_TIMEOUT_SQL = text(f"SET LOCAL lock_timeout = '{RENEWAL_LOCK_TIMEOUT_MS}ms'")

# The heartbeat. Owner-scoped for the same reason as COMPLETE_SQL and FAIL_SQL: a
# worker that was taken over must not be able to revive its claim by renewing, so a
# stale renewal matches zero rows instead of pushing a new owner's lease forward.
# The status guard keeps a finished row from being dragged back to a live lease.
# Zero rows is the whole signal — a renewal is not a claim and cannot take a job
# back — so the caller stops renewing rather than retrying.
RENEW_LEASE_SQL = text(
    """
    UPDATE jobs
    SET lease_expires_at = now() + make_interval(secs => :lease_seconds)
    WHERE id = :job_id AND lease_owner = :worker_id AND status = 'RUNNING'
    RETURNING id
    """
)

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

# The read side. ``created_at < now() - grace`` makes this a *candidate* list and
# nothing more: an ``INTENT`` row is old enough to be suspicious, which is all an
# unlocked read can safely say. Age alone cannot tell a stranded row from one whose
# handler is still alive — a handler that renews its lease outlives any grace period
# — so this query must never be the thing that decides to apply. The decision is
# ``LOCK_PENDING_EFFECT_SQL``; see there.
PENDING_EFFECTS_SQL = text("""
    SELECT job_id, effect_key, detail FROM job_effects
    WHERE status = 'INTENT' AND created_at < now() - make_interval(secs => :grace_seconds)
    ORDER BY created_at
    LIMIT :limit
""")

# The decision. Two guards, for two different races, both re-evaluated under the
# lock because both are mutable:
#
# * ``status = 'INTENT'`` is the reconciler-vs-reconciler guard. Every worker
#   reconciles on every cycle, so two of them will race for the same stranded row
#   as soon as there are two. ``FOR UPDATE`` — not ``SKIP LOCKED`` — is the fix: the
#   loser blocks until the winner commits, then re-checks its predicate against the
#   new row version, sees APPLIED and drops the row. The effect is applied once.
#   ``SKIP LOCKED`` would also apply once here, but it would hide contention;
#   blocking makes the second reconciler simply wait and then find nothing to do.
#
# * ``NOT (j.status = 'RUNNING' AND j.lease_expires_at > now())`` is the
#   reconciler-vs-handler guard: a live lease means a handler may still be about to
#   apply, so the reconciler must skip. This is why the guard is here and not in the
#   read: a heartbeat can land between an unlocked read and this statement, reviving
#   the lease, and a read-time check would already have decided to apply. Only a
#   check in the same statement that takes the lock sees the lease the apply is
#   competing with.
#
# ``FOR UPDATE OF je`` — not bare ``FOR UPDATE`` — names the effect row only. A bare
# ``FOR UPDATE`` would also lock the joined ``jobs`` row and block the heartbeat's
# ``UPDATE jobs SET lease_expires_at = ...`` for the whole apply-and-confirm. The
# heartbeat must stay cheap and unblocked: a handler renewing its lease is the thing
# that keeps this guard from firing, so serialising it behind the reconciler would
# defeat it.
#
# The ``created_at`` grace bound is not repeated here: it is immutable, so a row that
# satisfied it in the unlocked read still satisfies it now, and repeating it would
# look like a second guard while guarding nothing. ``age_seconds`` is still reported
# for the ``effect.reconciled`` event.
LOCK_PENDING_EFFECT_SQL = text("""
    SELECT je.job_id, je.effect_key, je.detail,
           EXTRACT(EPOCH FROM (now() - je.created_at))::bigint AS age_seconds
    FROM job_effects je
    JOIN jobs j ON j.id = je.job_id
    WHERE je.job_id = :job_id AND je.effect_key = :effect_key AND je.status = 'INTENT'
      AND NOT (j.status = 'RUNNING' AND j.lease_expires_at > now())
    FOR UPDATE OF je
""")


# ``WHERE idempotency_key IS NOT NULL`` is not decoration: PostgreSQL only uses a
# partial unique index as an arbiter when the statement repeats its predicate, and
# without it the insert raises InvalidColumnReference rather than conflicting. The
# index is partial because a unique index over a nullable column would make "no
# key" one shared key, which would let exactly one unkeyed job exist.
#
# Single statement on purpose: insert-or-nothing is the whole race guarantee. The
# read of the winner's row happens only in the caller's ``None`` branch, so two
# concurrent submissions of one key cannot both decide to insert. Unkeyed inserts go
# through the same statement — a NULL key never conflicts with the partial index, so
# one code path covers both and neither can drift from the other.
INSERT_JOB_SQL = text(
    """
    INSERT INTO jobs (id, type, payload, max_attempts, idempotency_key, request_fingerprint)
    VALUES (
        :id, :type, CAST(:payload AS jsonb), :max_attempts, :idempotency_key, :request_fingerprint
    )
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
    RETURNING *
    """
).bindparams(bindparam("payload", type_=JSONB(none_as_null=True)))

GET_JOB_BY_IDEMPOTENCY_KEY_SQL = text("""
    SELECT * FROM jobs WHERE idempotency_key = :idempotency_key
""")


def _execute_insert(session: Session, job: Job) -> Job | None:
    row = session.execute(
        INSERT_JOB_SQL,
        {
            "id": job.id,
            "type": job.type,
            "payload": job.payload,
            "max_attempts": job.max_attempts,
            "idempotency_key": job.idempotency_key,
            "request_fingerprint": job.request_fingerprint,
        },
    ).one_or_none()
    if row is None:
        return None
    return Job(**row._mapping)  # type: ignore[arg-type]


def insert_job(session: Session, job: Job) -> Job:
    """Insert an unkeyed job and return the stored row.

    The stored row is returned rather than the object passed in because
    ``created_at``, ``updated_at`` and ``next_attempt_at`` are server defaults: a
    directly executed INSERT does not populate them on the ORM object, and the 202
    body needs ``created_at``. A NULL key never conflicts under the partial index,
    so ``None`` here would mean the row was silently dropped.
    """
    inserted = _execute_insert(session, job)
    assert inserted is not None, "an unkeyed insert cannot conflict"
    return inserted


def insert_job_idempotent(
    session: Session,
    *,
    job: Job,
    idempotency_key: str,
    request_fingerprint: str,
) -> Job | None:
    """Insert ``job`` under ``idempotency_key``; ``None`` when the key is taken.

    ``None`` means another submission already owns the key — the caller must read
    that row and compare fingerprints, not retry the insert.
    """
    job.idempotency_key = idempotency_key
    job.request_fingerprint = request_fingerprint
    return _execute_insert(session, job)


def get_job_by_idempotency_key(session: Session, key: str) -> Job | None:
    row = session.execute(GET_JOB_BY_IDEMPOTENCY_KEY_SQL, {"idempotency_key": key}).one_or_none()
    if row is None:
        return None
    return Job(**row._mapping)  # type: ignore[arg-type]


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


def renew_lease(session: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> bool:
    """Push this worker's lease forward; ``False`` means the lease is gone.

    ``True`` is one row renewed. ``False`` is zero rows, and that is not a transient
    error to retry: it means this worker no longer owns the job — another worker
    reclaimed it, or it already reached a terminal state. The caller must stop
    renewing. Completion and failure are refused from here on by their own
    ``lease_owner`` guards, which ``_finish`` and ``_fail`` already report as
    ``job.lease_lost``.

    ``SET LOCAL lock_timeout`` bounds how long this statement may wait for the job
    row: a concurrent reclaim holds it under ``FOR UPDATE``, and the heartbeat's join
    is unbounded, so an unbounded wait here would hang the worker after its handler
    returned. The timeout raises rather than returning ``False`` — a lock is not a
    lost lease — so it propagates to the caller's error path. ``SET LOCAL`` scopes it
    to this transaction, so no other statement on the connection inherits it.
    """
    session.execute(SET_RENEWAL_LOCK_TIMEOUT_SQL)
    row = session.execute(
        RENEW_LEASE_SQL,
        {"job_id": job_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
    ).one_or_none()
    return row is not None


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
    """A **candidate** list of stranded ``INTENT`` effects older than ``grace_seconds``.

    Read-only and unlocked, so it can only apply an age filter: a row that is old
    enough to be suspicious, not a row proven abandoned. It deliberately does not
    check the job's lease — a live handler may still own a row that has outlived the
    grace window.

    The decision to apply belongs to :func:`lock_pending_effect`, which re-evaluates
    the mutable conditions (the effect's status and the job's lease) under the row
    lock. Nothing may be applied on the strength of this read alone, and the grace
    window is a cheap prefilter, not the safety property.
    """
    rows = session.execute(
        PENDING_EFFECTS_SQL, {"grace_seconds": grace_seconds, "limit": int(limit)}
    ).all()
    return [(row.job_id, row.effect_key, row.detail) for row in rows]


def lock_pending_effect(
    session: Session, *, job_id: uuid.UUID, effect_key: str
) -> tuple[int, dict | None] | None:
    """The authoritative gate: lock a still-``INTENT`` effect and decide whether to apply.

    Returns ``(age_seconds, detail)`` for the caller that won the lock, or ``None``
    when the effect must be left alone — it is no longer ``INTENT``, or its job holds
    a live lease and its handler may still apply it.

    Callers reach this after :func:`pending_effects`, whose unlocked read is only a
    candidate list. The guard that matters is here, evaluated in the same statement
    that takes the lock, because both conditions it reads are mutable and can change
    between a read and an apply: another reconciler confirming the effect, and a
    heartbeat renewing the job's lease. A read-time lease check would be racy exactly
    that way — a renewal landing after the check revives the lease and the duplicate
    still happens.

    The lock is held until the caller commits or rolls back, which is what makes the
    apply exclusive: a second reconciler blocks here, then re-evaluates the predicate
    against the committed row and gets ``None`` because the status is now
    ``APPLIED``. The lock covers the effect row only (``FOR UPDATE OF je``), never the
    job row, so a concurrent heartbeat is not blocked behind an apply.

    The guard protects the **live** case only. A handler that loses its lease and
    *then* applies can still duplicate; see the README's known limitations.
    """
    row = session.execute(
        LOCK_PENDING_EFFECT_SQL, {"job_id": job_id, "effect_key": effect_key}
    ).one_or_none()
    if row is None:
        return None
    return int(row.age_seconds), row.detail
