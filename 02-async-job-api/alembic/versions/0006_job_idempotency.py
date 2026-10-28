"""Idempotency-Key storage on jobs (PR-2-04b, ADR 0011).

Revision ID: 0006_job_idempotency
Revises: 0005_job_effects
Create Date: 2026-10-27

A client that retries ``POST /v1/jobs`` after a timeout must not create a second
job. Storing the submitted key on the row makes the replay a read: the insert is
``ON CONFLICT (idempotency_key) DO NOTHING``, so the first writer wins and every
later writer is told the key exists.

``ux_jobs_idempotency_key`` is **partial**: a unique index over a nullable column
would otherwise make "no key" an implicit key shared by every unkeyed row.
``ck_jobs_idempotency_pair`` keeps the key and the fingerprint from existing
without each other — a key with no fingerprint could never be checked for a
mismatched body, and a fingerprint with no key could never be looked up.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_job_idempotency"
down_revision: str | None = "0005_job_effects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = r"""
ALTER TABLE jobs
    ADD COLUMN idempotency_key TEXT,
    ADD COLUMN request_fingerprint TEXT;

CREATE UNIQUE INDEX ux_jobs_idempotency_key
    ON jobs (idempotency_key) WHERE idempotency_key IS NOT NULL;

ALTER TABLE jobs
    ADD CONSTRAINT ck_jobs_idempotency_pair CHECK (
        (idempotency_key IS NULL AND request_fingerprint IS NULL)
        OR (idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)
    );
"""

DOWNGRADE = """
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_idempotency_pair;
DROP INDEX ux_jobs_idempotency_key;
ALTER TABLE jobs DROP COLUMN request_fingerprint, DROP COLUMN idempotency_key;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
