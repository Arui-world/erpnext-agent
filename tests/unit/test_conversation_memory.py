from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.conversations.memory import (
    SUMMARY_CONTEXT_PREFIX,
    ConversationMemoryPolicy,
    ConversationMemoryService,
    SummaryGenerationError,
    should_summarize,
    summarizable_prefix,
    summary_batches,
    summary_for_context,
)
from erpnext_agent.conversations.repository import (
    ConversationMemory,
    ConversationRepository,
    StoredMessage,
    decode_memory,
    encode_memory,
)
from erpnext_agent.coordination import RedisLeaseClient


def message(sequence: int, content: str | None = None) -> StoredMessage:
    return StoredMessage(
        message_id=str(sequence),
        conversation_id="conversation",
        sequence=sequence,
        role="user" if sequence % 2 else "assistant",
        content=content or f"message-{sequence}",
        created_at=datetime.now(UTC),
    )


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool | None:
        del ex
        if nx and name in self.values:
            return None
        self.values[name] = value
        return True

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str,
    ) -> int:
        del script, numkeys
        key, owner = keys_and_args
        if self.values.get(key) == owner:
            del self.values[key]
            return 1
        return 0


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeRepository:
    def __init__(self, messages: list[StoredMessage]) -> None:
        self.messages = messages
        self.memory = ConversationMemory(content="", through_sequence=0)

    async def get_memory(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
    ) -> ConversationMemory:
        del session
        assert conversation_id == "conversation"
        return self.memory

    async def list_messages_after(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        after_sequence: int,
    ) -> list[StoredMessage]:
        del session
        assert conversation_id == "conversation"
        return [item for item in self.messages if item.sequence > after_sequence]

    async def save_memory(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        memory: ConversationMemory,
    ) -> ConversationMemory:
        del session
        assert conversation_id == "conversation"
        if memory.through_sequence > self.memory.through_sequence:
            self.memory = memory
        return self.memory


class FakeGenerator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, list[int]]] = []

    async def summarize(
        self,
        *,
        previous_summary: str,
        messages: list[StoredMessage],
        max_chars: int,
    ) -> str:
        del max_chars
        self.calls.append((previous_summary, [item.sequence for item in messages]))
        if self.fail:
            raise SummaryGenerationError("summary failed")
        return f"{previous_summary} summary-through-{messages[-1].sequence}".strip()


def service(
    repository: FakeRepository,
    generator: FakeGenerator,
    redis: FakeRedis | None = None,
) -> ConversationMemoryService:
    return ConversationMemoryService(
        redis=cast(RedisLeaseClient, redis or FakeRedis()),
        generator=generator,
        policy=ConversationMemoryPolicy(
            enabled=True,
            trigger_messages=8,
            trigger_chars=10_000,
            keep_recent_messages=4,
            source_max_chars=100,
            summary_max_chars=100,
            lock_ttl_seconds=30,
        ),
        repository=cast(ConversationRepository, repository),
    )


def test_memory_encoding_round_trip_and_legacy_fallback() -> None:
    memory = ConversationMemory(
        content="用户要查库存",
        through_sequence=12,
        updated_at=datetime.now(UTC),
    )
    assert decode_memory(encode_memory(memory)) == memory
    assert decode_memory("旧版纯文本摘要") == ConversationMemory(
        content="旧版纯文本摘要",
        through_sequence=0,
    )


def test_summary_trigger_and_prefix_only_cover_complete_old_turns() -> None:
    messages = [message(index) for index in range(1, 13)]
    assert should_summarize(messages, trigger_messages=8, trigger_chars=10_000)
    assert [item.sequence for item in summarizable_prefix(messages, keep_recent_messages=5)] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]


def test_summary_batches_split_only_after_assistant_messages() -> None:
    messages = [message(index, "x" * 4) for index in range(1, 9)]
    batches = summary_batches(messages, max_chars=16)
    assert [[item.sequence for item in batch] for batch in batches] == [
        [1, 2, 3, 4],
        [5, 6, 7, 8],
    ]
    assert all(batch[-1].role == "assistant" for batch in batches)


def test_summary_context_respects_the_available_character_budget() -> None:
    budget = len(SUMMARY_CONTEXT_PREFIX) + 10
    result = summary_for_context("很长的摘要" * 20, max_chars=budget)
    assert result.startswith(SUMMARY_CONTEXT_PREFIX)
    assert len(result) <= budget
    assert summary_for_context("摘要", max_chars=len(SUMMARY_CONTEXT_PREFIX)) == ""


@pytest.mark.asyncio
async def test_service_persists_summary_and_keeps_recent_messages() -> None:
    repository = FakeRepository([message(index) for index in range(1, 13)])
    generator = FakeGenerator()
    session = FakeSession()
    context = await service(repository, generator).prepare_context(
        cast(AsyncSession, session),
        conversation_id="conversation",
        current_message="continue",
        max_messages=20,
        max_chars=1000,
    )
    assert repository.memory.through_sequence == 8
    assert repository.memory.content == "summary-through-8"
    assert [item.sequence for item in context.messages] == [9, 10, 11, 12]
    assert context.summary == SUMMARY_CONTEXT_PREFIX + "summary-through-8"
    assert len(repository.messages) == 12
    assert session.commits == 1


@pytest.mark.asyncio
async def test_service_incrementally_merges_previous_summary() -> None:
    repository = FakeRepository([message(index) for index in range(1, 17)])
    repository.memory = ConversationMemory(content="summary-through-8", through_sequence=8)
    generator = FakeGenerator()
    await service(repository, generator).prepare_context(
        cast(AsyncSession, FakeSession()),
        conversation_id="conversation",
        current_message="continue",
        max_messages=20,
        max_chars=1000,
    )
    assert generator.calls == [("summary-through-8", [9, 10, 11, 12])]
    assert repository.memory == replace(
        repository.memory,
        content="summary-through-8 summary-through-12",
        through_sequence=12,
    )


@pytest.mark.asyncio
async def test_summary_failure_falls_back_without_losing_chat_context() -> None:
    repository = FakeRepository([message(index) for index in range(1, 13)])
    session = FakeSession()
    context = await service(repository, FakeGenerator(fail=True)).prepare_context(
        cast(AsyncSession, session),
        conversation_id="conversation",
        current_message="continue",
        max_messages=4,
        max_chars=1000,
    )
    assert repository.memory.through_sequence == 0
    assert [item.sequence for item in context.messages] == [9, 10, 11, 12]
    assert context.summary == ""
    assert session.rollbacks == 1
