"""Effect outbox (PR-2-04, falsification case 4).

Revision ID: 0005_job_effects
Revises: 0004_job_retry
Create Date: 2026-10-27

A reclaimed job re-runs its handler, so an external side effect that succeeded
before the crash would happen twice. ``job_effects`` is the outbox that makes the
duplicate detectable: a handler inserts ``(job_id, effect_key)`` *before* applying
the effect, and the primary key turns a second claim into a no-op. A row left
``INTENT`` by a crash is swept up by the worker's reconciler once it is older than
``EFFECT_RECONCILE_GRACE_SECONDS``.

``ck_effect_applied_at`` keeps status and timestamp from disagreeing, so
"applied" is always a time a reconciler or handler can report. The partial index
covers the reconciler's read: it only ever looks at ``INTENT`` rows ordered by
age.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_job_effects"
down_revision: str | None = "0004_job_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = r"""
CREATE TABLE job_effects (
    job_id     UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    effect_key TEXT        NOT NULL,
    status     TEXT        NOT NULL DEFAULT 'INTENT',
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, effect_key),
    CONSTRAINT ck_effect_status CHECK (status IN ('INTENT','APPLIED')),
    CONSTRAINT ck_effect_applied_at CHECK (
        (status = 'INTENT'  AND applied_at IS NULL) OR
        (status = 'APPLIED' AND applied_at IS NOT NULL)
    )
);

CREATE INDEX ix_job_effects_pending
    ON job_effects (created_at) WHERE status = 'INTENT';
"""

DOWNGRADE = """
DROP INDEX ix_job_effects_pending;
DROP TABLE job_effects;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
