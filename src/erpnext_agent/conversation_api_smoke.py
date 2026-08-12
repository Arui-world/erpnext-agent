from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import uuid

import httpx
from redis.asyncio import Redis
from sqlalchemy import delete

from erpnext_agent.auth.models import OAuthCredentialRecord
from erpnext_agent.auth.oauth_client import OAuthTokenSet
from erpnext_agent.auth.session_store import AgentSession, SessionStore
from erpnext_agent.auth.token_store import TokenStore
from erpnext_agent.config import get_settings
from erpnext_agent.conversations.models import ConversationRecord
from erpnext_agent.db import create_engine, create_session_factory


async def run_smoke() -> dict[str, object]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    session_store = SessionStore(
        redis,
        settings.session_ttl_seconds,
        settings.session_secret.get_secret_value(),
    )
    token_store = TokenStore(
        encryption_key=settings.token_encryption_key.get_secret_value(),
        key_version=settings.token_encryption_key_version,
        client_id=settings.oauth_client_id,
    )
    credential_ids: list[str] = []
    agent_sessions: list[AgentSession] = []
    authorization_cache_keys: list[str] = []
    conversation_id: str | None = None

    async def temporary_identity(user_id: str) -> AgentSession:
        access_token = secrets.token_urlsafe(32)
        binding_id = str(uuid.uuid4())
        async with factory() as db:
            credential_id = await token_store.create(
                db,
                binding_id=binding_id,
                site=settings.erpnext_site,
                oauth_subject=user_id,
                user_id=user_id,
                token=OAuthTokenSet(
                    access_token=access_token,
                    refresh_token=None,
                    token_type="Bearer",  # noqa: S106 - OAuth scheme identifier
                    scope="all openid",
                    expires_in=None,
                ),
            )
            await db.commit()
        credential_ids.append(credential_id)
        agent_session = await session_store.create(
            credential_id=credential_id,
            binding_id=binding_id,
            site=settings.erpnext_site,
            user_id=user_id,
        )
        agent_sessions.append(agent_session)
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        cache_key = f"agent:introspection:{credential_id}:{token_hash}"
        await redis.setex(cache_key, 300, '{"active":true}')
        authorization_cache_keys.append(cache_key)
        return agent_session

    def headers(agent_session: AgentSession) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": agent_session.csrf_token,
        }

    def cookies(agent_session: AgentSession) -> dict[str, str]:
        return {settings.session_cookie_name: agent_session.session_id}

    def require_status(response: httpx.Response, expected: int) -> None:
        if response.status_code != expected:
            raise AssertionError(
                f"{response.request.method} {response.request.url.path} returned "
                f"{response.status_code}, expected {expected}"
            )

    try:
        owner = await temporary_identity("conversation-owner@example.invalid")
        other = await temporary_identity("conversation-other@example.invalid")
        base_url = os.getenv("SMOKE_AGENT_URL", "http://agent:8001").rstrip("/")
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            created_response = await http.post(
                f"{settings.api_prefix}/chat/conversations",
                headers=headers(owner),
                cookies=cookies(owner),
                json={"mode": "model"},
            )
            require_status(created_response, 201)
            conversation_id = str(created_response.json()["conversation_id"])

            renamed_response = await http.patch(
                f"{settings.api_prefix}/chat/conversations/{conversation_id}",
                headers=headers(owner),
                cookies=cookies(owner),
                json={"mode": "model", "title": "  API\n生命周期验收  "},
            )
            require_status(renamed_response, 200)
            if renamed_response.json()["title"] != "API 生命周期验收":
                raise AssertionError("Renamed title was not normalized")

            cross_user_response = await http.patch(
                f"{settings.api_prefix}/chat/conversations/{conversation_id}",
                headers=headers(other),
                cookies=cookies(other),
                json={"mode": "model", "title": "unauthorized"},
            )
            require_status(cross_user_response, 404)

            cross_user_delete_response = await http.delete(
                f"{settings.api_prefix}/chat/conversations/{conversation_id}",
                headers=headers(other),
                cookies=cookies(other),
                params={"mode": "model"},
            )
            require_status(cross_user_delete_response, 404)

            list_response = await http.get(
                f"{settings.api_prefix}/chat/conversations",
                headers={"Accept": "application/json"},
                cookies=cookies(owner),
                params={"mode": "model"},
            )
            require_status(list_response, 200)
            listed = {
                item["conversation_id"]: item
                for item in list_response.json()["conversations"]
            }
            if listed.get(conversation_id, {}).get("title") != "API 生命周期验收":
                raise AssertionError("Renamed title was not returned by the list API")

            deleted_response = await http.delete(
                f"{settings.api_prefix}/chat/conversations/{conversation_id}",
                headers=headers(owner),
                cookies=cookies(owner),
                params={"mode": "model"},
            )
            require_status(deleted_response, 204)

            history_response = await http.get(
                f"{settings.api_prefix}/chat/history",
                headers={"Accept": "application/json"},
                cookies=cookies(owner),
                params={"mode": "model", "conversation_id": conversation_id},
            )
            require_status(history_response, 404)

        return {
            "created": True,
            "renamed": True,
            "cross_user_update_status": 404,
            "cross_user_delete_status": 404,
            "soft_deleted": True,
            "deleted_history_status": 404,
        }
    finally:
        for agent_session in agent_sessions:
            await session_store.delete(agent_session.session_id)
        if authorization_cache_keys:
            await redis.delete(*authorization_cache_keys)
        async with factory() as db:
            if conversation_id is not None:
                await db.execute(
                    delete(ConversationRecord).where(
                        ConversationRecord.conversation_id == conversation_id
                    )
                )
            if credential_ids:
                await db.execute(
                    delete(OAuthCredentialRecord).where(
                        OAuthCredentialRecord.credential_id.in_(credential_ids)
                    )
                )
            await db.commit()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
