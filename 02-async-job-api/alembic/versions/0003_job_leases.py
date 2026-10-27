"""Job lease columns and constraints (PR-2-02).

Revision ID: 0003_job_leases
Revises: 0002_jobs
Create Date: 2026-10-27

Adds the claim/lease protocol columns per docs/decisions/0010-postgres-job-store.md.
``ix_jobs_claim`` from 0002_jobs already covers the claim query.
Lease expiry recovery lands in PR-2-03; this revision only records the expiry.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_job_leases"
down_revision: str | None = "0002_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = r"""
ALTER TABLE jobs
    ADD COLUMN lease_owner TEXT,
    ADD COLUMN lease_expires_at TIMESTAMPTZ,
    ADD COLUMN claimed_at TIMESTAMPTZ,
    ADD COLUMN finished_at TIMESTAMPTZ;

ALTER TABLE jobs
    ADD CONSTRAINT ck_jobs_lease_pair CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    ADD CONSTRAINT ck_jobs_running_has_lease CHECK (
        status <> 'RUNNING' OR lease_owner IS NOT NULL
    ),
    ADD CONSTRAINT ck_jobs_terminal_has_finished CHECK (
        status NOT IN ('SUCCEEDED','FAILED') OR finished_at IS NOT NULL
    );

CREATE INDEX ix_jobs_lease_expiry
    ON jobs (lease_expires_at)
    WHERE status = 'RUNNING';
"""

DOWNGRADE = """
DROP INDEX ix_jobs_lease_expiry;

ALTER TABLE jobs
    DROP CONSTRAINT ck_jobs_terminal_has_finished,
    DROP CONSTRAINT ck_jobs_running_has_lease,
    DROP CONSTRAINT ck_jobs_lease_pair;

ALTER TABLE jobs
    DROP COLUMN finished_at,
    DROP COLUMN claimed_at,
    DROP COLUMN lease_expires_at,
    DROP COLUMN lease_owner;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
