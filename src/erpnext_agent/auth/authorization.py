from __future__ import annotations

import hashlib
import json

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.auth.oauth_client import OAuthClient, OAuthError
from erpnext_agent.auth.session_store import AgentSession, SessionStore
from erpnext_agent.auth.token_refresh import TokenRefreshError, TokenRefreshService
from erpnext_agent.auth.token_store import StoredCredential, TokenStore


class AuthorizationRevokedError(RuntimeError):
    pass


class AuthorizationUnavailableError(RuntimeError):
    pass


class AuthorizationService:
    """Fail-closed authorization guard shared by every protected Agent route."""

    def __init__(
        self,
        *,
        oauth: OAuthClient,
        refresh: TokenRefreshService,
        token_store: TokenStore,
        session_store: SessionStore,
        redis: Redis,
        client_id: str,
        cache_seconds: int,
    ) -> None:
        self._oauth = oauth
        self._refresh = refresh
        self._token_store = token_store
        self._session_store = session_store
        self._redis = redis
        self._client_id = client_id
        self._cache_seconds = cache_seconds

    async def validate(
        self,
        db: AsyncSession,
        agent_session: AgentSession,
    ) -> StoredCredential:
        if await self._redis.get(f"agent:revoked-binding:{agent_session.binding_id}"):
            await self._session_store.delete_by_binding(agent_session.binding_id)
            raise AuthorizationRevokedError("ERPNext OAuth authorization was revoked")
        try:
            credential = await self._refresh.get_valid(db, agent_session.credential_id)
        except TokenRefreshError as exc:
            await self._session_store.delete_by_binding(agent_session.binding_id)
            raise AuthorizationRevokedError(str(exc)) from exc

        if (
            credential.binding_id != agent_session.binding_id
            or credential.site != agent_session.site
            or credential.user_id.casefold() != agent_session.user_id.casefold()
        ):
            await self._revoke(db, agent_session)
            raise AuthorizationRevokedError("Agent session identity binding is invalid")

        cache_key = self._cache_key(credential)
        if self._cache_seconds and await self._redis.get(cache_key):
            return credential

        try:
            result = await self._oauth.introspect(credential.access_token)
        except OAuthError as exc:
            raise AuthorizationUnavailableError(str(exc)) from exc
        if not result.active or result.client_id != self._client_id:
            await self._revoke(db, agent_session)
            raise AuthorizationRevokedError("ERPNext OAuth authorization was revoked")

        if self._cache_seconds:
            await self._redis.setex(
                cache_key,
                self._cache_seconds,
                json.dumps({"active": True}, separators=(",", ":")),
            )
        return credential

    async def invalidate_binding_cache(self, binding_id: str) -> None:
        # The current cache key includes the access-token hash, so deleting sessions
        # and revoking the local credential is sufficient.  This marker prevents a
        # concurrent request from treating a just-validated token as authoritative.
        await self._redis.setex(f"agent:revoked-binding:{binding_id}", 86_400, "1")

    async def _revoke(self, db: AsyncSession, agent_session: AgentSession) -> None:
        await self._token_store.revoke_by_binding(db, agent_session.binding_id)
        await db.commit()
        await self.invalidate_binding_cache(agent_session.binding_id)
        await self._session_store.delete_by_binding(agent_session.binding_id)

    @staticmethod
    def _cache_key(credential: StoredCredential) -> str:
        token_hash = hashlib.sha256(credential.access_token.encode()).hexdigest()
        return f"agent:introspection:{credential.credential_id}:{token_hash}"
