from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from erpnext_agent.db import Base


class ConversationRecord(Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index(
            "ix_chat_conversation_owner_updated",
            "site",
            "user_id",
            "mode",
            "updated_at",
        ),
    )

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_chat_message_conversation_sequence",
        ),
        Index("ix_chat_message_conversation_created", "conversation_id", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
