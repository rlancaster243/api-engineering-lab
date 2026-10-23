"""Initial schema: users and saved_items (spec section 3.2)

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-22

Replaces the unmerged ``0001_saved_items`` revision from this branch, which predated
the spec. A local database built from that revision fails ``alembic upgrade`` with an
unknown-revision error and must be recreated; nothing was deployed (ADR 0008).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQL statements are kept identical to the spec DDL (only comments differ), so the two
# can be compared directly.
UPGRADE = r"""
CREATE TABLE users (
    id UUID PRIMARY KEY,
    issuer VARCHAR(512),
    subject VARCHAR(512),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_external_identity UNIQUE (issuer, subject),
    CONSTRAINT ck_external_identity_pair CHECK (
        (issuer IS NULL AND subject IS NULL)
        OR (issuer IS NOT NULL AND subject IS NOT NULL)
    )
);

-- UNIQUE (issuer, subject) treats NULLs as distinct, so it cannot constrain the
-- local seed identity. This index allows at most one row with a NULL issuer.
CREATE UNIQUE INDEX uq_users_local_seed ON users ((1)) WHERE issuer IS NULL;

CREATE TABLE saved_items (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    content_id VARCHAR(255) NOT NULL,
    title VARCHAR(200),
    url TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_content UNIQUE (user_id, content_id),
    -- Trim set (space, tab, LF, CR, FF, VT) must equal the API trim set (section 4.2).
    CONSTRAINT ck_content_id_normalized CHECK (
        length(btrim(content_id, E' \t\n\r\f\x0b')) > 0
        AND content_id = btrim(content_id, E' \t\n\r\f\x0b')
    ),
    CONSTRAINT ck_title_nonempty CHECK (
        title IS NULL OR length(btrim(title, E' \t\n\r\f\x0b')) > 0
    ),
    CONSTRAINT ck_url_shape CHECK (
        url IS NULL OR (length(url) <= 2048 AND url ~* '^https?://')
    ),
    CONSTRAINT ck_notes_length CHECK (notes IS NULL OR length(notes) <= 2000),
    CONSTRAINT ck_updated_after_created CHECK (updated_at >= created_at)
);

CREATE INDEX ix_saved_items_user_created_id
    ON saved_items (user_id, created_at DESC, id DESC);

-- The single local development owner (spec section 3.2). Idempotent.
INSERT INTO users (id) VALUES ('00000000-0000-0000-0000-000000000001') ON CONFLICT DO NOTHING;
"""

DOWNGRADE = """
DROP TABLE saved_items;
DROP TABLE users;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
