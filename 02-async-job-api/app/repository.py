"""Data access layer — all SQL lives here."""

import uuid

from sqlalchemy.orm import Session

from app.models import Job


def insert_job(session: Session, job: Job) -> None:
    session.add(job)


def get_job_by_id(session: Session, job_id: uuid.UUID) -> Job | None:
    return session.get(Job, job_id)
