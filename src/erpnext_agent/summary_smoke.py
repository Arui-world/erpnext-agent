from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from agentscope.message import AssistantMsg, Msg, UserMsg
from redis.asyncio import Redis
from sqlalchemy import delete

from erpnext_agent.agents.factory import ConfiguredAgentFactory
from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.config import get_settings
from erpnext_agent.conversations.memory import (
    AgentSummaryGenerator,
    ConversationMemoryPolicy,
    ConversationMemoryService,
)
from erpnext_agent.conversations.models import ConversationRecord
from erpnext_agent.conversations.repository import ConversationRepository, StoredMessage
from erpnext_agent.db import create_engine, create_session_factory


def _model_messages(
    summary: str,
    recent: list[StoredMessage],
    current: str,
) -> list[Msg]:
    messages: list[Msg] = [AssistantMsg(name="conversation_memory", content=summary)]
    for item in recent:
        if item.role == "user":
            messages.append(UserMsg(name="user", content=item.content))
        else:
            messages.append(AssistantMsg(name="assistant", content=item.content))
    messages.append(UserMsg(name="user", content=current))
    return messages


async def run_smoke() -> dict[str, Any]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    repository = ConversationRepository()
    agent_factory = ConfiguredAgentFactory(settings)
    memory_service = ConversationMemoryService(
        redis=redis,
        generator=AgentSummaryGenerator(agent_factory),
        policy=ConversationMemoryPolicy(
            enabled=True,
            trigger_messages=8,
            trigger_chars=4_000,
            keep_recent_messages=4,
            source_max_chars=8_000,
            summary_max_chars=2_000,
            lock_ttl_seconds=120,
        ),
        repository=repository,
    )
    memory_code = f"SUMMARY-{secrets.token_hex(4).upper()}"
    conversation_id: str | None = None
    try:
        async with factory() as db:
            conversation = await repository.resolve_or_create(
                db,
                conversation_id=None,
                site=settings.erpnext_site,
                user_id="summary-smoke@local",
                mode="model",
            )
            conversation_id = conversation.conversation_id
            turns = [
                ("user", f"请记住项目代号 {memory_code}，这是后续必须保留的关键信息。"),
                ("assistant", f"已记住项目代号 {memory_code}。"),
                ("user", "项目目标是验证长对话摘要。"),
                ("assistant", "已记录项目目标。"),
                ("user", "重要决定是保留完整原始消息。"),
                ("assistant", "已记录不删除原始消息的决定。"),
                ("user", "摘要应该与最近消息一起进入上下文。"),
                ("assistant", "已记录摘要和最近消息的组装方式。"),
                ("user", "这是应当保留的最近消息一。"),
                ("assistant", "已保留最近消息一。"),
                ("user", "这是应当保留的最近消息二。"),
                ("assistant", "已保留最近消息二。"),
            ]
            for role, content in turns:
                await repository.append_message(
                    db,
                    conversation_id=conversation_id,
                    role="user" if role == "user" else "assistant",
                    content=content,
                )
            await db.commit()

            context = await memory_service.prepare_context(
                db,
                conversation_id=conversation_id,
                current_message="项目代号是什么？只回复代号。",
                max_messages=20,
                max_chars=24_000,
            )
            memory = await repository.get_memory(db, conversation_id=conversation_id)
            all_messages = await repository.list_messages(
                db,
                conversation_id=conversation_id,
                limit=100,
            )
            reply = await agent_factory.build_model_chat_agent().reply(
                _model_messages(
                    context.summary,
                    context.messages,
                    "项目代号是什么？只回复代号。",
                )
            )
            recalled = assistant_text(reply).strip()
            if memory.through_sequence != 8:
                raise RuntimeError("Summary did not cover the expected complete old turns")
            if len(all_messages) != 12:
                raise RuntimeError("Summary unexpectedly removed original messages")
            if [item.sequence for item in context.messages] != [9, 10, 11, 12]:
                raise RuntimeError("Summary did not retain the expected recent messages")
            if memory_code not in memory.content or memory_code not in recalled:
                raise RuntimeError("The summarized memory did not preserve the project code")
            result = {
                "summary_through_sequence": memory.through_sequence,
                "original_message_count": len(all_messages),
                "recent_sequences": [item.sequence for item in context.messages],
                "summary_chars": len(memory.content),
                "memory_code_recalled": True,
            }
            await db.execute(
                delete(ConversationRecord).where(
                    ConversationRecord.conversation_id == conversation_id
                )
            )
            await db.commit()
            conversation_id = None
            return result
    finally:
        if conversation_id is not None:
            async with factory() as cleanup:
                await cleanup.execute(
                    delete(ConversationRecord).where(
                        ConversationRecord.conversation_id == conversation_id
                    )
                )
                await cleanup.commit()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False))  # noqa: T201


if __name__ == "__main__":
    main()
