"""Business logic — enforces the commit-before-202 rule."""

import uuid

from sqlalchemy.orm import Session

from app import repository
from app.models import Job


def submit_job(session: Session, job_type: str, payload: dict, max_attempts: int) -> Job:
    """Insert a job and commit so the row is durable before 202 is returned."""
    job = Job(
        id=uuid.uuid4(),
        type=job_type,
        payload=payload,
        status="PENDING",
        attempt=0,
        max_attempts=max_attempts,
    )
    repository.insert_job(session, job)
    session.commit()
    return job


def get_job(session: Session, job_id: uuid.UUID) -> Job | None:
    return repository.get_job_by_id(session, job_id)
