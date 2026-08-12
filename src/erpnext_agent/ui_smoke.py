from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from redis.asyncio import Redis

from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.config import get_settings


async def _stream_model_reply(client: httpx.AsyncClient, csrf_token: str) -> str:
    chunks: list[str] = []
    event_name = "message"
    completed = False
    async with client.stream(
        "POST",
        "/api/v1/chat/model/stream",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "请只回复：前端对话连接成功。"},
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
                continue
            if not line.startswith("data:"):
                continue
            payload: dict[str, Any] = json.loads(line.removeprefix("data:").strip())
            if event_name == "text_delta":
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
    return reply


async def run_smoke() -> str:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    store = SessionStore(
        redis,
        settings.session_ttl_seconds,
        settings.session_secret.get_secret_value(),
    )
    session = await store.create(
        credential_id="ui-smoke-model-only",
        binding_id="00000000-0000-0000-0000-000000000004",
        site=settings.erpnext_site,
        user_id="ui-smoke@local",
    )
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8001",
            cookies={settings.session_cookie_name: session.session_id},
            timeout=60,
        ) as client:
            page = await client.get("/")
            page.raise_for_status()
            if "ERPNext Agent" not in page.text:
                raise RuntimeError("Chat UI content is missing")
            return await _stream_model_reply(client, session.csrf_token)
    finally:
        await store.delete(session.session_id)
        await redis.aclose()


def main() -> None:
    print(asyncio.run(run_smoke()))  # noqa: T201 - operator-facing smoke output


if __name__ == "__main__":
    main()
