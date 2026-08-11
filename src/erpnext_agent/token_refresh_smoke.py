from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from erpnext_agent.auth.models import OAuthCredentialRecord
from erpnext_agent.auth.oauth_client import OAuthClient
from erpnext_agent.auth.token_refresh import TokenRefreshService
from erpnext_agent.auth.token_store import TokenStore
from erpnext_agent.config import get_settings
from erpnext_agent.db import create_engine, create_session_factory
from erpnext_agent.mcp.adapter import ERPNextMCPAdapter


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def run_smoke() -> dict[str, Any]:
    settings = get_settings()
    user_id = _required_env("TOKEN_REFRESH_SMOKE_USER_ID")
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.mcp_http_timeout_seconds),
        follow_redirects=False,
    )
    store = TokenStore(
        encryption_key=settings.token_encryption_key.get_secret_value(),
        key_version=settings.token_encryption_key_version,
        client_id=settings.oauth_client_id,
    )
    refresh = TokenRefreshService(
        store=store,
        oauth=OAuthClient(settings, http),
        redis=redis,
        leeway_seconds=settings.oauth_refresh_leeway_seconds,
        lock_ttl_seconds=settings.oauth_refresh_lock_ttl_seconds,
        wait_seconds=settings.oauth_refresh_wait_seconds,
        poll_seconds=settings.oauth_refresh_poll_seconds,
    )
    adapter = ERPNextMCPAdapter(
        url=settings.effective_mcp_url,
        http=http,
        verify_contract=settings.mcp_verify_tool_contract,
        host_header=settings.erpnext_host_header,
    )
    try:
        async with factory() as db:
            credential_id = await db.scalar(
                select(OAuthCredentialRecord.credential_id)
                .where(
                    OAuthCredentialRecord.site == settings.erpnext_site,
                    OAuthCredentialRecord.user_id == user_id,
                    OAuthCredentialRecord.client_id == settings.oauth_client_id,
                    OAuthCredentialRecord.revoked_at.is_(None),
                )
                .order_by(OAuthCredentialRecord.updated_at.desc())
                .limit(1)
            )
            if credential_id is None:
                raise RuntimeError(f"No active OAuth credential found for {user_id}")
            before = await store.get(db, credential_id)
            after = await refresh.refresh_after_auth_failure(db, before)
            actual_user = await adapter.current_user(after.access_token)
            if actual_user.casefold() != user_id.casefold():
                raise RuntimeError("Refreshed token belongs to a different ERPNext user")
            if after.credential_id != before.credential_id:
                raise RuntimeError("Token refresh replaced the stable credential identity")
            if after.updated_at <= before.updated_at:
                raise RuntimeError("Token refresh did not persist a newer credential version")
            return {
                "user": actual_user,
                "credential_id_unchanged": True,
                "credential_version_advanced": True,
                "refresh_token_preserved": bool(after.refresh_token),
                "expires_at": after.expires_at,
            }
    finally:
        await http.aclose()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, default=str))  # noqa: T201


if __name__ == "__main__":
    main()
