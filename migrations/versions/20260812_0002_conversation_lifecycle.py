"""Add persistent titles and soft deletion to conversations.

Revision ID: 20260812_0002
Revises: 20260812_0001
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0002"
down_revision: str | None = "20260812_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_conversations",
        sa.Column("title", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "chat_conversations",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_chat_conversations_deleted_at",
        "chat_conversations",
        ["deleted_at"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Conversation lifecycle migration is forward-only; restore a database backup "
        "to roll back"
    )
