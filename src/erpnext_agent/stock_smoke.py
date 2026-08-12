from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from erpnext_agent.auth.models import OAuthCredentialRecord
from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.config import get_settings
from erpnext_agent.conversations.repository import ConversationRepository
from erpnext_agent.db import create_engine, create_session_factory

EXPECTED_TOOL = "erpnext_get_item_stock_by_warehouses"


@dataclass(frozen=True, slots=True)
class StockStreamReply:
    conversation_id: str
    text: str
    tool_calls: list[str]


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def _latest_credential_id(user_id: str) -> str:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            credential_id = await db.scalar(
                select(OAuthCredentialRecord.credential_id)
                .where(
                    OAuthCredentialRecord.site == settings.erpnext_site,
                    OAuthCredentialRecord.user_id == user_id,
                    OAuthCredentialRecord.revoked_at.is_(None),
                )
                .order_by(OAuthCredentialRecord.updated_at.desc())
                .limit(1)
            )
    finally:
        await engine.dispose()
    if credential_id is None:
        raise RuntimeError(f"No active OAuth credential found for {user_id}")
    return credential_id


async def _stream_stock_reply(
    client: httpx.AsyncClient,
    csrf_token: str,
    message: str,
) -> StockStreamReply:
    conversation_id: str | None = None
    text_chunks: list[str] = []
    tool_calls: list[str] = []
    event_name = "message"
    completed = False
    async with client.stream(
        "POST",
        "/api/v1/chat/stream",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": message},
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
                    conversation_id = value
            elif event_name == "tool_call_start":
                value = payload.get("tool_name")
                if isinstance(value, str):
                    tool_calls.append(value)
            elif event_name == "text_delta":
                text_chunks.append(str(payload.get("delta", "")))
            elif event_name == "error":
                raise RuntimeError(str(payload.get("message", "Agent stream failed")))
            elif event_name == "done":
                completed = True

    text = "".join(text_chunks).strip()
    if not completed:
        raise RuntimeError("Agent stream did not emit a done event")
    if conversation_id is None:
        raise RuntimeError("Agent stream did not identify the conversation")
    if not text:
        raise RuntimeError("Agent stream returned no text")
    return StockStreamReply(conversation_id, text, tool_calls)


async def _persisted_roles(conversation_id: str, user_id: str) -> list[str]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            repository = ConversationRepository()
            conversation = await repository.get_owned(
                db,
                conversation_id=conversation_id,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="agent",
            )
            if conversation is None:
                raise RuntimeError("Stock conversation was not persisted")
            messages = await repository.list_messages(
                db,
                conversation_id=conversation_id,
                limit=10,
            )
            return [message.role for message in messages]
    finally:
        await engine.dispose()


async def run_smoke() -> dict[str, Any]:
    settings = get_settings()
    user_id = _required_env("STOCK_SMOKE_USER_ID")
    item_code = _required_env("STOCK_SMOKE_ITEM_CODE")
    expected_text = os.getenv("STOCK_SMOKE_EXPECT_TEXT", "")
    credential_id = await _latest_credential_id(user_id)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    store = SessionStore(
        redis,
        settings.session_ttl_seconds,
        settings.session_secret.get_secret_value(),
    )
    session = await store.create(
        credential_id=credential_id,
        binding_id="00000000-0000-0000-0000-000000000003",
        site=settings.erpnext_site,
        user_id=user_id,
    )
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8001",
            cookies={settings.session_cookie_name: session.session_id},
            timeout=120,
        ) as client:
            reply = await _stream_stock_reply(
                client,
                session.csrf_token,
                f"{item_code}的库存",
            )
        if reply.tool_calls != [EXPECTED_TOOL]:
            raise RuntimeError(
                f"Expected exactly one {EXPECTED_TOOL} call, got {reply.tool_calls}"
            )
        if expected_text and expected_text not in reply.text:
            raise RuntimeError(f"Reply does not contain expected text: {expected_text}")
        roles = await _persisted_roles(reply.conversation_id, user_id)
        if roles != ["user", "assistant"]:
            raise RuntimeError(f"Unexpected persisted roles: {roles}")
        return {
            "conversation_id": reply.conversation_id,
            "tool_calls": reply.tool_calls,
            "reply": reply.text,
            "database_roles": roles,
        }
    finally:
        await store.delete(session.session_id)
        await redis.aclose()


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False))  # noqa: T201


if __name__ == "__main__":
    main()
