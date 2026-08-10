from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from redis.asyncio import Redis

from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.config import get_settings
from erpnext_agent.conversations.repository import ConversationRepository
from erpnext_agent.db import create_engine, create_session_factory


@dataclass(frozen=True, slots=True)
class StreamReply:
    conversation_id: str
    text: str


async def _stream_model_reply(
    client: httpx.AsyncClient,
    csrf_token: str,
    *,
    message: str,
    conversation_id: str | None = None,
) -> StreamReply:
    chunks: list[str] = []
    event_name = "message"
    resolved_conversation_id: str | None = None
    completed = False
    body: dict[str, str | None] = {
        "message": message,
        "conversation_id": conversation_id,
    }
    async with client.stream(
        "POST",
        "/api/v1/chat/model/stream",
        headers={"X-CSRF-Token": csrf_token},
        json=body,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
                continue
            if not line.startswith("data:"):
                continue
            payload: dict[str, Any] = json.loads(line.removeprefix("data:").strip())
            if event_name == "conversation":
                value = payload.get("conversation_id")
                if isinstance(value, str):
                    resolved_conversation_id = value
            elif event_name == "text_delta":
                chunks.append(str(payload.get("delta", "")))
            elif event_name == "error":
                raise RuntimeError(str(payload.get("message", "Model stream failed")))
            elif event_name == "done":
                completed = True

    reply = "".join(chunks).strip()
    if not completed:
        raise RuntimeError("Model stream did not emit a done event")
    if not reply:
        raise RuntimeError("Model stream returned no text")
    if resolved_conversation_id is None:
        raise RuntimeError("Model stream did not identify the conversation")
    return StreamReply(resolved_conversation_id, reply)


async def run_smoke() -> dict[str, Any]:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    store = SessionStore(
        redis,
        settings.session_ttl_seconds,
        settings.session_secret.get_secret_value(),
    )
    test_user = "memory-smoke@local"
    session = await store.create(
        credential_id="memory-smoke-model-only",
        site=settings.erpnext_site,
        user_id=test_user,
    )
    memory_code = f"MEM-{secrets.token_hex(5).upper()}"
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8001",
            cookies={settings.session_cookie_name: session.session_id},
            timeout=120,
        ) as client:
            first = await _stream_model_reply(
                client,
                session.csrf_token,
                message=f"请记住随机码 {memory_code}。只回复：已记住。",
            )
            second = await _stream_model_reply(
                client,
                session.csrf_token,
                message="我上一条让你记住的随机码是什么？只回复随机码。",
                conversation_id=first.conversation_id,
            )
            history_response = await client.get(
                "/api/v1/chat/history",
                params={
                    "mode": "model",
                    "conversation_id": first.conversation_id,
                },
            )
            history_response.raise_for_status()
            history_payload = history_response.json()
            api_messages = history_payload.get("messages", [])
            latest_history_response = await client.get(
                "/api/v1/chat/history",
                params={"mode": "model"},
            )
            latest_history_response.raise_for_status()
            latest_history = latest_history_response.json()
            conversation_list_response = await client.get(
                "/api/v1/chat/conversations",
                params={"mode": "model"},
            )
            conversation_list_response.raise_for_status()
            conversations = conversation_list_response.json().get("conversations", [])
            listed_conversation = next(
                (
                    item
                    for item in conversations
                    if item.get("conversation_id") == first.conversation_id
                ),
                None,
            )
            new_conversation_response = await client.post(
                "/api/v1/chat/conversations",
                headers={"X-CSRF-Token": session.csrf_token},
                json={"mode": "model"},
            )
            new_conversation_response.raise_for_status()
            new_conversation = new_conversation_response.json()

        engine = create_engine(settings.database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as db:
                repository = ConversationRepository()
                conversation = await repository.get_owned(
                    db,
                    conversation_id=first.conversation_id,
                    site=settings.erpnext_site,
                    user_id=test_user,
                    mode="model",
                )
                if conversation is None:
                    raise RuntimeError("Conversation was not persisted")
                messages = await repository.list_messages(
                    db,
                    conversation_id=first.conversation_id,
                    limit=10,
                )
        finally:
            await engine.dispose()

        if memory_code not in second.text:
            raise RuntimeError("The model did not recall the server-side conversation history")
        if len(messages) != 4:
            raise RuntimeError(f"Expected 4 persisted messages, found {len(messages)}")
        if not isinstance(api_messages, list) or len(api_messages) != 4:
            raise RuntimeError("History API did not restore all four persisted messages")
        if latest_history.get("conversation_id") != first.conversation_id:
            raise RuntimeError("Latest-conversation history did not survive a fresh request")
        if not isinstance(listed_conversation, dict):
            raise RuntimeError("Conversation list did not include the persisted conversation")
        if listed_conversation.get("message_count") != 4:
            raise RuntimeError("Conversation list returned an incorrect message count")
        if new_conversation.get("conversation_id") == first.conversation_id:
            raise RuntimeError("New-conversation API reused an existing conversation")
        return {
            "conversation_id": first.conversation_id,
            "database_message_count": len(messages),
            "database_roles": [message.role for message in messages],
            "history_api_message_count": len(api_messages),
            "conversation_list_message_count": listed_conversation["message_count"],
            "new_conversation_created": True,
            "memory_code_recalled": True,
            "first_reply": first.text,
            "second_reply": second.text,
        }
    finally:
        await store.delete(session.session_id)
        await redis.aclose()


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False))  # noqa: T201


if __name__ == "__main__":
    main()
