from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.auth.authorization import (
    AuthorizationRevokedError,
    AuthorizationService,
    AuthorizationUnavailableError,
)
from erpnext_agent.auth.oauth_client import OAuthError, OAuthIntrospection
from erpnext_agent.auth.session_store import AgentSession
from erpnext_agent.auth.token_store import StoredCredential

BINDING_ID = "00000000-0000-0000-0000-000000000050"


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, seconds: int, value: str) -> bool:
        assert seconds > 0
        self.values[key] = value
        return True


class FakeOAuth:
    def __init__(self, result: OAuthIntrospection | Exception) -> None:
        self.result = result
        self.calls = 0

    async def introspect(self, access_token: str) -> OAuthIntrospection:
        assert access_token == "access-token"  # noqa: S105
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeRefresh:
    def __init__(self, credential: StoredCredential) -> None:
        self.credential = credential

    async def get_valid(
        self,
        db: AsyncSession,
        credential_id: str,
    ) -> StoredCredential:
        del db
        assert credential_id == self.credential.credential_id
        return self.credential


class FakeTokenStore:
    def __init__(self) -> None:
        self.revoked: list[str] = []

    async def revoke_by_binding(self, db: AsyncSession, binding_id: str) -> list[str]:
        del db
        self.revoked.append(binding_id)
        return ["credential-1"]


class FakeSessionStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_by_binding(self, binding_id: str) -> list[str]:
        self.deleted.append(binding_id)
        return ["session-1"]


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def credential() -> StoredCredential:
    return StoredCredential(
        credential_id="credential-1",
        binding_id=BINDING_ID,
        site="dev.localhost",
        oauth_subject="Administrator",
        user_id="Administrator",
        access_token="access-token",  # noqa: S106
        refresh_token="refresh-token",  # noqa: S106
        scope="all openid",
        expires_at=None,
        updated_at=datetime.now(UTC),
        revoked_at=None,
    )


def agent_session() -> AgentSession:
    return AgentSession(
        session_id="session-1",
        credential_id="credential-1",
        binding_id=BINDING_ID,
        site="dev.localhost",
        user_id="Administrator",
        csrf_token="csrf-token",  # noqa: S106
        created_at=datetime.now(UTC).isoformat(),
    )


def service(oauth: FakeOAuth) -> tuple[AuthorizationService, FakeTokenStore, FakeSessionStore]:
    stored = credential()
    tokens = FakeTokenStore()
    sessions = FakeSessionStore()
    return (
        AuthorizationService(
            oauth=cast(Any, oauth),
            refresh=cast(Any, FakeRefresh(stored)),
            token_store=cast(Any, tokens),
            session_store=cast(Any, sessions),
            redis=cast(Any, FakeRedis()),
            client_id="client-id",
            cache_seconds=5,
        ),
        tokens,
        sessions,
    )


@pytest.mark.asyncio
async def test_active_introspection_is_cached_for_protected_requests() -> None:
    oauth = FakeOAuth(OAuthIntrospection(active=True, client_id="client-id"))
    guard, tokens, sessions = service(oauth)
    db = cast(AsyncSession, FakeDB())

    await guard.validate(db, agent_session())
    await guard.validate(db, agent_session())

    assert oauth.calls == 1
    assert tokens.revoked == []
    assert sessions.deleted == []


@pytest.mark.asyncio
async def test_revoked_binding_removes_only_its_sessions() -> None:
    oauth = FakeOAuth(OAuthIntrospection(active=False, client_id="client-id"))
    guard, tokens, sessions = service(oauth)

    with pytest.raises(AuthorizationRevokedError):
        await guard.validate(cast(AsyncSession, FakeDB()), agent_session())

    assert tokens.revoked == [BINDING_ID]
    assert sessions.deleted == [BINDING_ID]


@pytest.mark.asyncio
async def test_introspection_outage_fails_closed_without_revoking_credentials() -> None:
    oauth = FakeOAuth(OAuthError("ERPNext unavailable"))
    guard, tokens, sessions = service(oauth)

    with pytest.raises(AuthorizationUnavailableError):
        await guard.validate(cast(AsyncSession, FakeDB()), agent_session())

    assert tokens.revoked == []
    assert sessions.deleted == []
