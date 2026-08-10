from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.conversations.models import ChatMessageRecord, ConversationRecord

ConversationMode = Literal["model", "agent"]
MessageRole = Literal["user", "assistant"]


class ConversationNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class StoredMessage:
    message_id: str
    conversation_id: str
    sequence: int
    role: MessageRole
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    conversation_id: str
    mode: ConversationMode
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationRepository:
    async def resolve_or_create(
        self,
        session: AsyncSession,
        *,
        conversation_id: str | None,
        site: str,
        user_id: str,
        mode: ConversationMode,
    ) -> ConversationRecord:
        if conversation_id is not None:
            conversation = await self.get_owned(
                session,
                conversation_id=conversation_id,
                site=site,
                user_id=user_id,
                mode=mode,
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            return conversation

        now = datetime.now(UTC)
        conversation = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            site=site,
            user_id=user_id,
            mode=mode,
            created_at=now,
            updated_at=now,
        )
        session.add(conversation)
        await session.flush()
        return conversation

    async def get_owned(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        site: str,
        user_id: str,
        mode: ConversationMode,
    ) -> ConversationRecord | None:
        conversation: ConversationRecord | None = await session.scalar(
            select(ConversationRecord).where(
                ConversationRecord.conversation_id == conversation_id,
                ConversationRecord.site == site,
                ConversationRecord.user_id == user_id,
                ConversationRecord.mode == mode,
            )
        )
        return conversation

    async def latest_owned(
        self,
        session: AsyncSession,
        *,
        site: str,
        user_id: str,
        mode: ConversationMode,
    ) -> ConversationRecord | None:
        conversation: ConversationRecord | None = await session.scalar(
            select(ConversationRecord)
            .where(
                ConversationRecord.site == site,
                ConversationRecord.user_id == user_id,
                ConversationRecord.mode == mode,
            )
            .order_by(desc(ConversationRecord.updated_at))
            .limit(1)
        )
        return conversation

    async def list_owned(
        self,
        session: AsyncSession,
        *,
        site: str,
        user_id: str,
        mode: ConversationMode,
        limit: int,
    ) -> list[ConversationSummary]:
        conversations = list(
            await session.scalars(
                select(ConversationRecord)
                .where(
                    ConversationRecord.site == site,
                    ConversationRecord.user_id == user_id,
                    ConversationRecord.mode == mode,
                )
                .order_by(desc(ConversationRecord.updated_at))
                .limit(limit)
            )
        )
        summaries: list[ConversationSummary] = []
        for conversation in conversations:
            first_user_message = await session.scalar(
                select(ChatMessageRecord.content)
                .where(
                    ChatMessageRecord.conversation_id == conversation.conversation_id,
                    ChatMessageRecord.role == "user",
                )
                .order_by(ChatMessageRecord.sequence)
                .limit(1)
            )
            message_count = await session.scalar(
                select(func.count(ChatMessageRecord.message_id)).where(
                    ChatMessageRecord.conversation_id == conversation.conversation_id
                )
            )
            summaries.append(
                ConversationSummary(
                    conversation_id=conversation.conversation_id,
                    mode=mode,
                    title=conversation_title(first_user_message),
                    message_count=int(message_count or 0),
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                )
            )
        return summaries

    async def append_message(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        role: MessageRole,
        content: str,
    ) -> StoredMessage:
        conversation = await session.scalar(
            select(ConversationRecord)
            .where(ConversationRecord.conversation_id == conversation_id)
            .with_for_update()
        )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)

        last_sequence = await session.scalar(
            select(func.max(ChatMessageRecord.sequence)).where(
                ChatMessageRecord.conversation_id == conversation_id
            )
        )
        now = datetime.now(UTC)
        record = ChatMessageRecord(
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            sequence=(last_sequence or 0) + 1,
            role=role,
            content=content,
            created_at=now,
        )
        conversation.updated_at = now
        session.add(record)
        await session.flush()
        return _stored_message(record)

    async def list_messages(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        limit: int,
    ) -> list[StoredMessage]:
        records = list(
            await session.scalars(
                select(ChatMessageRecord)
                .where(ChatMessageRecord.conversation_id == conversation_id)
                .order_by(desc(ChatMessageRecord.sequence))
                .limit(limit)
            )
        )
        records.reverse()
        return [_stored_message(record) for record in records]

    async def load_context(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        max_messages: int,
        max_chars: int,
    ) -> list[StoredMessage]:
        messages = await self.list_messages(
            session,
            conversation_id=conversation_id,
            limit=max_messages,
        )
        return trim_context(messages, max_chars=max_chars)


def trim_context(messages: list[StoredMessage], *, max_chars: int) -> list[StoredMessage]:
    """Keep the newest contiguous messages that fit in the model context budget."""
    selected: list[StoredMessage] = []
    used = 0
    for message in reversed(messages):
        size = len(message.content)
        if used + size > max_chars:
            break
        selected.append(message)
        used += size
    selected.reverse()
    return selected


def conversation_title(content: str | None, *, max_length: int = 42) -> str:
    normalized = " ".join((content or "").split())
    if not normalized:
        return "新对话"
    if len(normalized) <= max_length:
        return normalized
    return normalized[:max_length].rstrip() + "…"


def _stored_message(record: ChatMessageRecord) -> StoredMessage:
    role: MessageRole = "user" if record.role == "user" else "assistant"
    return StoredMessage(
        message_id=record.message_id,
        conversation_id=record.conversation_id,
        sequence=record.sequence,
        role=role,
        content=record.content,
        created_at=record.created_at,
    )
