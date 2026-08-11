import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.auth.oauth_client import OAuthClient, OAuthError, OAuthTokenSet
from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.auth.token_refresh import TokenRefreshError, TokenRefreshService
from erpnext_agent.auth.token_store import StoredCredential, TokenStore
from erpnext_agent.coordination import RedisLeaseClient
from erpnext_agent.mcp.adapter import (
    ERPNextMCPAdapter,
    MCPEnvelope,
    MCPTransportError,
)
from erpnext_agent.mcp.refreshing_caller import RefreshingMCPCaller

TEST_REFRESH_TOKEN = "refresh-token"  # noqa: S105 - inert test fixture
OLD_ACCESS_TOKEN = "access-token-old"  # noqa: S105 - inert test fixture
NEW_ACCESS_TOKEN = "access-token-new"  # noqa: S105 - inert test fixture
IGNORED_ACCESS_TOKEN = "ignored"  # noqa: S105 - adapter intentionally ignores this fixture


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
        if self.values.get(key) != owner:
            return 0
        del self.values[key]
        return 1


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeTokenStore:
    def __init__(self, credential: StoredCredential) -> None:
        self.credential = credential

    async def get(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> StoredCredential:
        del session
        if credential_id != self.credential.credential_id:
            raise AssertionError("unexpected credential")
        return self.credential

    async def upsert(
        self,
        session: AsyncSession,
        *,
        site: str,
        oauth_subject: str,
        user_id: str,
        token: OAuthTokenSet,
    ) -> str:
        del session
        assert (site, oauth_subject, user_id) == (
            self.credential.site,
            self.credential.oauth_subject,
            self.credential.user_id,
        )
        self.credential = replace(
            self.credential,
            access_token=token.access_token,
            refresh_token=token.refresh_token or self.credential.refresh_token,
            scope=token.scope,
            expires_at=datetime.now(UTC) + timedelta(seconds=token.expires_in or 0),
            updated_at=self.credential.updated_at + timedelta(seconds=1),
        )
        return self.credential.credential_id


class FakeOAuth:
    def __init__(self, *, fail: bool = False, delay: float = 0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        assert refresh_token == TEST_REFRESH_TOKEN
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise OAuthError("safe refresh failure")
        return OAuthTokenSet(
            access_token=f"access-token-{self.calls}",
            refresh_token=None,
            token_type="Bearer",  # noqa: S106 - OAuth token type identifier
            scope="all openid",
            expires_in=3600,
        )


def credential(
    *,
    expires_delta: int = -60,
    refresh_token: str | None = TEST_REFRESH_TOKEN,
) -> StoredCredential:
    now = datetime.now(UTC)
    return StoredCredential(
        credential_id="credential-id",
        site="dev.localhost",
        oauth_subject="Administrator",
        user_id="Administrator",
        access_token=OLD_ACCESS_TOKEN,
        refresh_token=refresh_token,
        scope="all openid",
        expires_at=now + timedelta(seconds=expires_delta),
        updated_at=now,
        revoked_at=None,
    )


def refresh_service(
    store: FakeTokenStore,
    oauth: FakeOAuth,
    redis: FakeRedis,
) -> TokenRefreshService:
    return TokenRefreshService(
        store=cast(TokenStore, store),
        oauth=cast(OAuthClient, oauth),
        redis=cast(RedisLeaseClient, redis),
        leeway_seconds=120,
        lock_ttl_seconds=30,
        wait_seconds=2,
        poll_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_valid_token_is_not_refreshed() -> None:
    store = FakeTokenStore(credential(expires_delta=600))
    oauth = FakeOAuth()
    result = await refresh_service(store, oauth, FakeRedis()).get_valid(
        cast(AsyncSession, FakeSession()),
        "credential-id",
    )
    assert result.access_token == OLD_ACCESS_TOKEN
    assert oauth.calls == 0


@pytest.mark.asyncio
async def test_expiring_token_is_refreshed_and_old_refresh_token_is_preserved() -> None:
    store = FakeTokenStore(credential())
    oauth = FakeOAuth()
    session = FakeSession()
    result = await refresh_service(store, oauth, FakeRedis()).get_valid(
        cast(AsyncSession, session),
        "credential-id",
    )
    assert result.access_token == "access-token-1"  # noqa: S105
    assert result.refresh_token == TEST_REFRESH_TOKEN
    assert oauth.calls == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_concurrent_refresh_uses_one_distributed_single_flight() -> None:
    store = FakeTokenStore(credential())
    oauth = FakeOAuth(delay=0.05)
    redis = FakeRedis()
    service = refresh_service(store, oauth, redis)
    first, second = await asyncio.gather(
        service.get_valid(cast(AsyncSession, FakeSession()), "credential-id"),
        service.get_valid(cast(AsyncSession, FakeSession()), "credential-id"),
    )
    assert first.access_token == second.access_token == "access-token-1"  # noqa: S105
    assert oauth.calls == 1
    assert not redis.values


@pytest.mark.asyncio
async def test_refresh_failure_requires_new_login_without_exposing_token() -> None:
    store = FakeTokenStore(credential())
    session = FakeSession()
    with pytest.raises(TokenRefreshError, match="sign in again") as raised:
        await refresh_service(store, FakeOAuth(fail=True), FakeRedis()).get_valid(
            cast(AsyncSession, session),
            "credential-id",
        )
    assert "refresh-token" not in str(raised.value)
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_missing_refresh_token_requires_new_login() -> None:
    store = FakeTokenStore(credential(refresh_token=None))
    with pytest.raises(TokenRefreshError, match="unavailable"):
        await refresh_service(store, FakeOAuth(), FakeRedis()).get_valid(
            cast(AsyncSession, FakeSession()),
            "credential-id",
        )


class SequenceAdapter:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.tokens: list[str] = []

    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope:
        del name, arguments, discover_first
        self.tokens.append(access_token)
        if len(self.tokens) <= self.failures:
            raise MCPTransportError("auth failed", code="MCP_AUTH_FAILED")
        return MCPEnvelope(data={"ok": True}, meta={})


class FakeRefreshService:
    def __init__(self, result: StoredCredential | None) -> None:
        self.result = result
        self.calls = 0

    async def refresh_after_auth_failure(
        self,
        session: AsyncSession,
        failed: StoredCredential,
    ) -> StoredCredential:
        del session, failed
        self.calls += 1
        if self.result is None:
            raise TokenRefreshError("please sign in again")
        return self.result


class FakeSessionStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, raw_id: str) -> None:
        self.deleted.append(raw_id)


def refreshing_caller(
    adapter: SequenceAdapter,
    refresh: FakeRefreshService,
    sessions: FakeSessionStore,
) -> RefreshingMCPCaller:
    refreshed = replace(
        credential(),
        access_token=NEW_ACCESS_TOKEN,
        updated_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    refresh.result = refresh.result or refreshed
    return RefreshingMCPCaller(
        adapter=cast(ERPNextMCPAdapter, adapter),
        refresh_service=cast(TokenRefreshService, refresh),
        db=cast(AsyncSession, FakeSession()),
        credential=credential(),
        session_store=cast(SessionStore, sessions),
        agent_session_id="agent-session",
    )


@pytest.mark.asyncio
async def test_mcp_auth_failure_refreshes_and_retries_exactly_once() -> None:
    adapter = SequenceAdapter(failures=1)
    refresh = FakeRefreshService(
        result=replace(credential(), access_token=NEW_ACCESS_TOKEN)
    )
    sessions = FakeSessionStore()
    result = await refreshing_caller(adapter, refresh, sessions).call_tool(
        access_token=IGNORED_ACCESS_TOKEN,
        name="erpnext_get_list",
        arguments={},
    )
    assert result.data == {"ok": True}
    assert adapter.tokens == [OLD_ACCESS_TOKEN, NEW_ACCESS_TOKEN]
    assert refresh.calls == 1
    assert sessions.deleted == []


@pytest.mark.asyncio
async def test_second_mcp_auth_failure_stops_retry_and_expires_session() -> None:
    adapter = SequenceAdapter(failures=2)
    refresh = FakeRefreshService(
        result=replace(credential(), access_token=NEW_ACCESS_TOKEN)
    )
    sessions = FakeSessionStore()
    with pytest.raises(MCPTransportError, match="auth failed"):
        await refreshing_caller(adapter, refresh, sessions).call_tool(
            access_token=IGNORED_ACCESS_TOKEN,
            name="erpnext_get_list",
            arguments={},
        )
    assert len(adapter.tokens) == 2
    assert refresh.calls == 1
    assert sessions.deleted == ["agent-session"]


@pytest.mark.asyncio
async def test_mcp_refresh_failure_expires_session() -> None:
    adapter = SequenceAdapter(failures=1)
    refresh = FakeRefreshService(result=None)
    sessions = FakeSessionStore()
    caller = RefreshingMCPCaller(
        adapter=cast(ERPNextMCPAdapter, adapter),
        refresh_service=cast(TokenRefreshService, refresh),
        db=cast(AsyncSession, FakeSession()),
        credential=credential(),
        session_store=cast(SessionStore, sessions),
        agent_session_id="agent-session",
    )
    with pytest.raises(MCPTransportError, match="sign in again"):
        await caller.call_tool(
            access_token=IGNORED_ACCESS_TOKEN,
            name="erpnext_get_list",
            arguments={},
        )
    assert sessions.deleted == ["agent-session"]
