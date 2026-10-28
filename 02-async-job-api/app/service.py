"""Business logic — enforces the commit-before-202 rule and the idempotency protocol."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import repository
from app.errors import ApiError, ErrorCode
from app.models import Job


@dataclass(frozen=True)
class SubmitOutcome:
    """One shape for the three submit paths.

    ``replayed`` is the only thing the router needs in order to tell a fresh job
    from a replayed one; comparing ids or reading a status string would be guesswork.
    """

    job: Job
    replayed: bool


def request_fingerprint(job_type: str, payload: dict, max_attempts: int) -> str:
    """SHA-256 over a canonical form, so key order in the client body cannot cause a
    spurious 409."""
    canonical = json.dumps(
        {"type": job_type, "payload": payload, "max_attempts": max_attempts},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ``submit_job``'s ``request_fingerprint`` parameter shadows the function above for
# the length of that body, so it calls this alias rather than the shadowed name.
_fingerprint = request_fingerprint


def submit_job(
    session: Session,
    job_type: str,
    payload: dict,
    max_attempts: int,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> SubmitOutcome:
    """Insert a job and commit so the row is durable before 202 is returned.

    Without a key this is the plain insert. With one, the insert is
    ``ON CONFLICT DO NOTHING``: a returned row means this call created the job; an
    empty result means another submission already owns the key, so the existing row
    is read and its fingerprint compared. A replay writes nothing — it is a read,
    and writing on it would mutate the record it is replaying.

    ``request_fingerprint`` accepts a digest the caller already computed. When it is
    omitted the service computes it, so the canonicalisation has exactly one
    definition and the router never hashes a request body.
    """
    fingerprint = None
    if idempotency_key is not None:
        fingerprint = request_fingerprint or _fingerprint(job_type, payload, max_attempts)

    job = Job(
        id=uuid.uuid4(),
        type=job_type,
        payload=payload,
        status="PENDING",
        attempt=0,
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )

    if idempotency_key is None:
        created = repository.insert_job(session, job)
        session.commit()
        return SubmitOutcome(job=created, replayed=False)

    assert fingerprint is not None  # implied by the branch above
    created = repository.insert_job_idempotent(
        session,
        job=job,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    if created is not None:
        session.commit()
        return SubmitOutcome(job=created, replayed=False)

    # Nothing was inserted, so end the transaction before reading: the winner's row
    # is then visible on a fresh snapshot and no lock is held across the read.
    session.commit()
    existing = repository.get_job_by_idempotency_key(session, idempotency_key)
    if existing is None:  # pragma: no cover - a committed key cannot vanish
        raise ApiError(ErrorCode.INTERNAL_ERROR)
    if existing.request_fingerprint != fingerprint:
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)
    return SubmitOutcome(job=existing, replayed=True)


def get_job(session: Session, job_id: uuid.UUID) -> Job | None:
    return repository.get_job_by_id(session, job_id)
