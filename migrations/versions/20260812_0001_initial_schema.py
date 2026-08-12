"""Create the initial managed Agent schema.

Revision ID: 20260812_0001
Revises: None
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "20260812_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MANAGED_COLUMNS: dict[str, frozenset[str]] = {
    "actions": frozenset(
        {
            "action_id",
            "session_id",
            "site",
            "requested_by",
            "tool_name",
            "canonical_arguments",
            "arguments_sha256",
            "preview",
            "source_versions",
            "idempotency_key",
            "status",
            "created_at",
            "expires_at",
            "decided_by",
            "decided_at",
            "executed_at",
            "mcp_trace_id",
            "result_reference",
            "failure_code",
            "failure_message",
        }
    ),
    "chat_conversations": frozenset(
        {
            "conversation_id",
            "site",
            "user_id",
            "mode",
            "summary",
            "created_at",
            "updated_at",
        }
    ),
    "chat_messages": frozenset(
        {
            "message_id",
            "conversation_id",
            "sequence",
            "role",
            "content",
            "created_at",
        }
    ),
    "oauth_device_credentials": frozenset(
        {
            "credential_id",
            "binding_id",
            "site",
            "client_id",
            "oauth_subject",
            "user_id",
            "scope",
            "token_type",
            "access_token_ciphertext",
            "refresh_token_ciphertext",
            "expires_at",
            "key_version",
            "created_at",
            "updated_at",
            "revoked_at",
        }
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = frozenset(inspector.get_table_names())
    managed = frozenset(_MANAGED_COLUMNS)
    present = existing & managed
    if present:
        if present != managed:
            missing = sorted(managed - present)
            raise RuntimeError(
                "Refusing to adopt a partial Agent schema; missing managed tables: "
                f"{missing}"
            )
        _validate_existing_schema(bind)
        return

    _create_chat_conversations()
    _create_chat_messages()
    _create_oauth_device_credentials()
    _create_actions()


def downgrade() -> None:
    raise RuntimeError(
        "The initial Agent migration is forward-only; restore a database backup to roll back"
    )


def _validate_existing_schema(bind: Connection) -> None:
    inspector = sa.inspect(bind)
    for table_name, expected_columns in _MANAGED_COLUMNS.items():
        actual_columns = frozenset(
            str(column["name"]) for column in inspector.get_columns(table_name)
        )
        if actual_columns != expected_columns:
            missing = sorted(expected_columns - actual_columns)
            unexpected = sorted(actual_columns - expected_columns)
            raise RuntimeError(
                f"Existing table {table_name!r} does not match the migration baseline; "
                f"missing columns={missing}, unexpected columns={unexpected}"
            )

    expected_primary_keys = {
        "actions": {"action_id"},
        "chat_conversations": {"conversation_id"},
        "chat_messages": {"message_id"},
        "oauth_device_credentials": {"credential_id"},
    }
    for table_name, expected in expected_primary_keys.items():
        actual = set(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
        if actual != expected:
            raise RuntimeError(f"Existing table {table_name!r} has an incompatible primary key")

    action_uniques = _unique_column_sets(inspector, "actions")
    if frozenset({"site", "requested_by", "idempotency_key"}) not in action_uniques:
        raise RuntimeError("Existing actions table is missing its idempotency uniqueness rule")
    device_uniques = _unique_column_sets(inspector, "oauth_device_credentials")
    if frozenset({"binding_id"}) not in device_uniques:
        raise RuntimeError("Existing device credential table is missing unique binding_id")
    message_uniques = _unique_column_sets(inspector, "chat_messages")
    if frozenset({"conversation_id", "sequence"}) not in message_uniques:
        raise RuntimeError("Existing chat_messages table is missing sequence uniqueness")

    foreign_keys = inspector.get_foreign_keys("chat_messages")
    if not any(
        set(key.get("constrained_columns") or []) == {"conversation_id"}
        and key.get("referred_table") == "chat_conversations"
        and str((key.get("options") or {}).get("ondelete", "")).upper() == "CASCADE"
        for key in foreign_keys
    ):
        raise RuntimeError("Existing chat_messages table is missing its cascade foreign key")


def _unique_column_sets(inspector: sa.Inspector, table_name: str) -> set[frozenset[str]]:
    constraints = {
        frozenset(str(column) for column in item.get("column_names") or [])
        for item in inspector.get_unique_constraints(table_name)
    }
    indexes = {
        frozenset(str(column) for column in item.get("column_names") or [])
        for item in inspector.get_indexes(table_name)
        if item.get("unique")
    }
    return constraints | indexes


def _create_chat_conversations() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("site", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index("ix_chat_conversations_site", "chat_conversations", ["site"])
    op.create_index("ix_chat_conversations_user_id", "chat_conversations", ["user_id"])
    op.create_index("ix_chat_conversations_mode", "chat_conversations", ["mode"])
    op.create_index("ix_chat_conversations_updated_at", "chat_conversations", ["updated_at"])
    op.create_index(
        "ix_chat_conversation_owner_updated",
        "chat_conversations",
        ["site", "user_id", "mode", "updated_at"],
    )


def _create_chat_messages() -> None:
    op.create_table(
        "chat_messages",
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_chat_message_conversation_sequence",
        ),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])
    op.create_index(
        "ix_chat_message_conversation_created",
        "chat_messages",
        ["conversation_id", "created_at"],
    )


def _create_oauth_device_credentials() -> None:
    op.create_table(
        "oauth_device_credentials",
        sa.Column("credential_id", sa.String(length=36), nullable=False),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("site", sa.String(length=255), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("oauth_subject", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("token_type", sa.String(length=32), nullable=False),
        sa.Column("access_token_ciphertext", sa.Text(), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("key_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("credential_id"),
    )
    op.create_index(
        "ix_oauth_device_credentials_binding_id",
        "oauth_device_credentials",
        ["binding_id"],
        unique=True,
    )
    op.create_index("ix_oauth_device_credentials_site", "oauth_device_credentials", ["site"])
    op.create_index(
        "ix_oauth_device_credentials_user_id",
        "oauth_device_credentials",
        ["user_id"],
    )


def _create_actions() -> None:
    op.create_table(
        "actions",
        sa.Column("action_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("site", sa.String(length=255), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("canonical_arguments", sa.JSON(), nullable=False),
        sa.Column("arguments_sha256", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("source_versions", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mcp_trace_id", sa.String(length=255), nullable=True),
        sa.Column("result_reference", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint(
            "site",
            "requested_by",
            "idempotency_key",
            name="uq_action_user_idempotency",
        ),
    )
    op.create_index("ix_actions_session_id", "actions", ["session_id"])
    op.create_index("ix_actions_site", "actions", ["site"])
    op.create_index("ix_actions_requested_by", "actions", ["requested_by"])
    op.create_index("ix_actions_status", "actions", ["status"])
    op.create_index("ix_actions_expires_at", "actions", ["expires_at"])
    op.create_index("ix_action_session_status", "actions", ["session_id", "status"])
