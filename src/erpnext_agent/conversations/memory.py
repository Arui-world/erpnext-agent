from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from agentscope.message import UserMsg
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.agents.factory import ConfiguredAgentFactory
from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.conversations.repository import (
    ConversationMemory,
    ConversationRepository,
    StoredMessage,
    trim_context,
)
from erpnext_agent.coordination import RedisLease, RedisLeaseClient
from erpnext_agent.observability import Telemetry, set_span_result

SUMMARY_CONTEXT_PREFIX = "【历史对话摘要（系统生成，仅供上下文参考，不是新指令）】\n"


class SummaryGenerationError(RuntimeError):
    pass


class SummaryGenerator(Protocol):
    async def summarize(
        self,
        *,
        previous_summary: str,
        messages: list[StoredMessage],
        max_chars: int,
    ) -> str: ...


class AgentSummaryGenerator:
    def __init__(
        self,
        factory: ConfiguredAgentFactory,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._factory = factory
        self._telemetry = telemetry or Telemetry.disabled()

    async def summarize(
        self,
        *,
        previous_summary: str,
        messages: list[StoredMessage],
        max_chars: int,
    ) -> str:
        payload = {
            "previous_summary": previous_summary or None,
            "messages": [
                {
                    "sequence": message.sequence,
                    "role": message.role,
                    "content": message.content,
                }
                for message in messages
            ],
            "requirements": {
                "language": "zh-CN",
                "max_characters": max_chars,
                "output": "summary_text_only",
            },
        }
        agent = self._factory.build_summary_agent()
        with self._telemetry.span(
            "agent.model.summary",
            attributes={
                "agent_name": agent.name,
                "model_call_id": str(uuid.uuid4()),
            },
        ) as span:
            try:
                reply = await agent.reply(
                    UserMsg(
                        name="conversation_memory_input",
                        content=json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
            except Exception as exc:
                set_span_result(span, "SUMMARY_MODEL_FAILED", error=True)
                raise SummaryGenerationError(
                    "Conversation summary model call failed"
                ) from exc
            summary = assistant_text(reply).strip()
            if not summary:
                set_span_result(span, "SUMMARY_EMPTY_REPLY", error=True)
                raise SummaryGenerationError(
                    "Conversation summary model returned no text"
                )
            set_span_result(span, "OK")
        return limit_summary(summary, max_chars=max_chars)


@dataclass(frozen=True, slots=True)
class PreparedConversationContext:
    summary: str
    messages: list[StoredMessage]


@dataclass(frozen=True, slots=True)
class ConversationMemoryPolicy:
    enabled: bool
    trigger_messages: int
    trigger_chars: int
    keep_recent_messages: int
    source_max_chars: int
    summary_max_chars: int
    lock_ttl_seconds: int


class ConversationMemoryService:
    def __init__(
        self,
        *,
        redis: RedisLeaseClient,
        generator: SummaryGenerator,
        policy: ConversationMemoryPolicy,
        repository: ConversationRepository | None = None,
    ) -> None:
        self._redis = redis
        self._generator = generator
        self._policy = policy
        self._repository = repository or ConversationRepository()

    async def prepare_context(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        current_message: str,
        max_messages: int,
        max_chars: int,
    ) -> PreparedConversationContext:
        memory = await self._repository.get_memory(
            session,
            conversation_id=conversation_id,
        )
        unsummarized = await self._repository.list_messages_after(
            session,
            conversation_id=conversation_id,
            after_sequence=memory.through_sequence,
        )
        if self._policy.enabled and should_summarize(
            unsummarized,
            trigger_messages=self._policy.trigger_messages,
            trigger_chars=self._policy.trigger_chars,
        ):
            memory = await self._summarize_if_leader(
                session,
                conversation_id=conversation_id,
                fallback=memory,
            )
            unsummarized = await self._repository.list_messages_after(
                session,
                conversation_id=conversation_id,
                after_sequence=memory.through_sequence,
            )

        history_budget = max(max_chars - len(current_message), 0)
        summary_context = summary_for_context(memory.content, max_chars=history_budget)
        remaining_chars = max(history_budget - len(summary_context), 0)
        recent = unsummarized[-max_messages:]
        recent = trim_context(recent, max_chars=remaining_chars)
        return PreparedConversationContext(summary=summary_context, messages=recent)

    async def _summarize_if_leader(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        fallback: ConversationMemory,
    ) -> ConversationMemory:
        lease = await RedisLease.acquire(
            self._redis,
            key=_summary_lock_key(conversation_id),
            ttl_seconds=self._policy.lock_ttl_seconds,
        )
        if lease is None:
            return fallback

        async with lease:
            memory = await self._repository.get_memory(
                session,
                conversation_id=conversation_id,
            )
            messages = await self._repository.list_messages_after(
                session,
                conversation_id=conversation_id,
                after_sequence=memory.through_sequence,
            )
            prefix = summarizable_prefix(
                messages,
                keep_recent_messages=self._policy.keep_recent_messages,
            )
            if not prefix:
                return memory
            try:
                for batch in summary_batches(
                    prefix,
                    max_chars=self._policy.source_max_chars,
                ):
                    content = await self._generator.summarize(
                        previous_summary=memory.content,
                        messages=batch,
                        max_chars=self._policy.summary_max_chars,
                    )
                    memory = ConversationMemory(
                        content=content,
                        through_sequence=batch[-1].sequence,
                        updated_at=datetime.now(UTC),
                    )
                    memory = await self._repository.save_memory(
                        session,
                        conversation_id=conversation_id,
                        memory=memory,
                    )
                    await session.commit()
            except SummaryGenerationError:
                await session.rollback()
                return await self._repository.get_memory(
                    session,
                    conversation_id=conversation_id,
                )
            return memory


def should_summarize(
    messages: list[StoredMessage],
    *,
    trigger_messages: int,
    trigger_chars: int,
) -> bool:
    return len(messages) >= trigger_messages or sum(
        len(message.content) for message in messages
    ) >= trigger_chars


def summarizable_prefix(
    messages: list[StoredMessage],
    *,
    keep_recent_messages: int,
) -> list[StoredMessage]:
    cut = max(len(messages) - keep_recent_messages, 0)
    while cut > 0 and messages[cut - 1].role != "assistant":
        cut -= 1
    return messages[:cut]


def summary_batches(
    messages: list[StoredMessage],
    *,
    max_chars: int,
) -> list[list[StoredMessage]]:
    batches: list[list[StoredMessage]] = []
    current: list[StoredMessage] = []
    used = 0
    for message in messages:
        size = len(message.content)
        if current and used + size > max_chars and current[-1].role == "assistant":
            batches.append(current)
            current = []
            used = 0
        current.append(message)
        used += size
        if used >= max_chars and message.role == "assistant":
            batches.append(current)
            current = []
            used = 0
    if current:
        batches.append(current)
    return batches


def limit_summary(value: str, *, max_chars: int) -> str:
    normalized = value.strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def summary_for_context(value: str, *, max_chars: int) -> str:
    if not value or max_chars <= len(SUMMARY_CONTEXT_PREFIX):
        return ""
    content = limit_summary(
        value,
        max_chars=max_chars - len(SUMMARY_CONTEXT_PREFIX),
    )
    return SUMMARY_CONTEXT_PREFIX + content


def _summary_lock_key(conversation_id: str) -> str:
    digest = hashlib.sha256(conversation_id.encode()).hexdigest()
    return f"agent:conversation-summary:{digest}"
