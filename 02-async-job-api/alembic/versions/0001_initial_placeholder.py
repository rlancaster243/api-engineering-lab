"""Initial revision placeholder (PR-2-00)

Revision ID: 0001_initial_placeholder
Revises:
Create Date: 2026-10-26

Job tables are added in PR-2-01; this revision establishes the Alembic chain.
"""

from collections.abc import Sequence

revision: str = "0001_initial_placeholder"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
