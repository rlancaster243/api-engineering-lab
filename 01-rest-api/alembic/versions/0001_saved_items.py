"""create users and saved_items

Revision ID: 0001_saved_items
Revises:
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_saved_items"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "saved_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_id", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_saved_items_user_id"),
        sa.UniqueConstraint("user_id", "content_id", name="uq_saved_items_user_content"),
    )
    op.create_index(
        "ix_saved_items_user_created_at",
        "saved_items",
        ["user_id", "created_at"],
    )
    op.execute(
        "INSERT INTO users (id, email) VALUES "
        "('00000000-0000-4000-8000-000000000001', 'dev@localhost')"
    )


def downgrade() -> None:
    op.drop_index("ix_saved_items_user_created_at", table_name="saved_items")
    op.drop_table("saved_items")
    op.drop_table("users")
