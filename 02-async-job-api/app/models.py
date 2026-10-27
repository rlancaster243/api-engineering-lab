"""ORM models for Alembic and SQLAlchemy queries."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')", name="ck_jobs_status"
        ),
        CheckConstraint("attempt >= 0 AND attempt <= max_attempts", name="ck_jobs_attempt_bounds"),
        CheckConstraint("max_attempts BETWEEN 1 AND 20", name="ck_jobs_max_attempts"),
        CheckConstraint(r"type ~ '^[a-z][a-z0-9_.-]{0,63}$'", name="ck_jobs_type_shape"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_jobs_payload_object"),
        CheckConstraint("updated_at >= created_at", name="ck_jobs_updated_after_created"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_jobs_lease_pair",
        ),
        CheckConstraint(
            "status <> 'RUNNING' OR lease_owner IS NOT NULL", name="ck_jobs_running_has_lease"
        ),
        CheckConstraint(
            "status NOT IN ('SUCCEEDED','FAILED') OR finished_at IS NOT NULL",
            name="ck_jobs_terminal_has_finished",
        ),
        Index("ix_jobs_claim", "status", "created_at", "id", postgresql_where="status = 'PENDING'"),
        Index(
            "ix_jobs_lease_expiry",
            "lease_expires_at",
            postgresql_where="status = 'RUNNING'",
        ),
    )
