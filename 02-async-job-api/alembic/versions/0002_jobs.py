"""Jobs table (PR-2-01).

Revision ID: 0002_jobs
Revises: 0001_initial_placeholder
Create Date: 2026-10-27

Adds the jobs table per docs/decisions/0010-postgres-job-store.md.
Lease columns land in PR-2-02 with the code that reads them.
Idempotency-Key storage lands in PR-2-04 per ADR 0011.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_jobs"
down_revision: str | None = "0001_initial_placeholder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = """
CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    result JSONB,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_jobs_status CHECK (
        status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')
    ),
    CONSTRAINT ck_jobs_attempt_bounds CHECK (attempt >= 0 AND attempt <= max_attempts),
    CONSTRAINT ck_jobs_max_attempts CHECK (max_attempts BETWEEN 1 AND 20),
    CONSTRAINT ck_jobs_type_shape CHECK (type ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    CONSTRAINT ck_jobs_payload_object CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX ix_jobs_claim ON jobs (status, created_at, id) WHERE status = 'PENDING';
"""

DOWNGRADE = """
DROP INDEX ix_jobs_claim;
DROP TABLE jobs;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
