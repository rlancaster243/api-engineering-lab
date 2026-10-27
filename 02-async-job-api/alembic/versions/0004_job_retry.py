"""Retry due-time column (PR-2-03).

Revision ID: 0004_job_retry
Revises: 0003_job_leases
Create Date: 2026-10-27

Adds ``next_attempt_at`` so a requeued job is not immediately claimable again.
Without it a job that always raises burns its whole attempt budget in a tight
loop and starves everything behind it.

Backfilled to ``created_at`` before the NOT NULL, so the migration is safe on a
non-empty table. No new index: the claim is two probes, and ``ix_jobs_claim``
plus ``ix_jobs_lease_expiry`` already cover them (see
docs/pr-2-03-recovery-spec.md section 3).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_job_retry"
down_revision: str | None = "0003_job_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = r"""
ALTER TABLE jobs
    ADD COLUMN next_attempt_at TIMESTAMPTZ;

UPDATE jobs SET next_attempt_at = created_at;

ALTER TABLE jobs
    ALTER COLUMN next_attempt_at SET NOT NULL,
    ALTER COLUMN next_attempt_at SET DEFAULT now();
"""

DOWNGRADE = """
ALTER TABLE jobs DROP COLUMN next_attempt_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
