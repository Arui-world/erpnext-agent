from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.auth.oauth_client import OAuthClient, OAuthError, OAuthTokenSet
from erpnext_agent.auth.token_store import (
    CredentialNotFoundError,
    StoredCredential,
    TokenStore,
)
from erpnext_agent.coordination import RedisLease, RedisLeaseClient


class TokenRefreshError(RuntimeError):
    """A safe error requiring a fresh browser OAuth authorization."""


class OAuthTokenRefresher(Protocol):
    async def refresh(self, refresh_token: str) -> OAuthTokenSet: ...


class TokenRefreshService:
    def __init__(
        self,
        *,
        store: TokenStore,
        oauth: OAuthClient,
        redis: RedisLeaseClient,
        leeway_seconds: int,
        lock_ttl_seconds: int,
        wait_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._store = store
        self._oauth: OAuthTokenRefresher = oauth
        self._redis = redis
        self._leeway = timedelta(seconds=leeway_seconds)
        self._lock_ttl_seconds = lock_ttl_seconds
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds

    async def get_valid(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> StoredCredential:
        credential = await self._load_active(session, credential_id)
        if not credential_expires_soon(credential, leeway=self._leeway):
            return credential
        return await self._refresh_single_flight(
            session,
            credential,
            forced=False,
        )

    async def refresh_after_auth_failure(
        self,
        session: AsyncSession,
        failed: StoredCredential,
    ) -> StoredCredential:
        return await self._refresh_single_flight(
            session,
            failed,
            forced=True,
        )

    async def _refresh_single_flight(
        self,
        session: AsyncSession,
        baseline: StoredCredential,
        *,
        forced: bool,
    ) -> StoredCredential:
        deadline = asyncio.get_running_loop().time() + self._wait_seconds
        lock_key = _refresh_lock_key(baseline.credential_id)
        while True:
            lease = await RedisLease.acquire(
                self._redis,
                key=lock_key,
                ttl_seconds=self._lock_ttl_seconds,
            )
            if lease is not None:
                async with lease:
                    latest = await self._load_active(session, baseline.credential_id)
                    if _another_request_refreshed(latest, baseline):
                        return latest
                    if not forced and not credential_expires_soon(
                        latest,
                        leeway=self._leeway,
                    ):
                        return latest
                    return await self._perform_refresh(session, latest)

            latest = await self._load_active(session, baseline.credential_id)
            if _another_request_refreshed(latest, baseline):
                return latest
            if not forced and not credential_expires_soon(latest, leeway=self._leeway):
                return latest
            if asyncio.get_running_loop().time() >= deadline:
                if not forced and not credential_is_expired(latest):
                    return latest
                raise TokenRefreshError("OAuth token refresh is busy; please retry")
            await asyncio.sleep(self._poll_seconds)

    async def _perform_refresh(
        self,
        session: AsyncSession,
        credential: StoredCredential,
    ) -> StoredCredential:
        if not credential.refresh_token:
            raise TokenRefreshError("OAuth refresh token is unavailable; please sign in again")
        try:
            token = await self._oauth.refresh(credential.refresh_token)
            await self._store.replace_tokens(
                session,
                credential_id=credential.credential_id,
                token=token,
            )
            await session.commit()
        except OAuthError as exc:
            await session.rollback()
            raise TokenRefreshError(
                "OAuth token refresh failed; please sign in again"
            ) from exc
        return await self._load_active(session, credential.credential_id)

    async def _load_active(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> StoredCredential:
        try:
            credential = await self._store.get(session, credential_id)
        except CredentialNotFoundError as exc:
            raise TokenRefreshError("OAuth credential was not found; please sign in again") from exc
        if credential.revoked_at is not None:
            raise TokenRefreshError("OAuth credential was revoked; please sign in again")
        return credential


def credential_expires_soon(
    credential: StoredCredential,
    *,
    leeway: timedelta,
    now: datetime | None = None,
) -> bool:
    if credential.expires_at is None:
        return False
    expires_at = credential.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (now or datetime.now(UTC)) + leeway


def credential_is_expired(
    credential: StoredCredential,
    *,
    now: datetime | None = None,
) -> bool:
    return credential_expires_soon(
        credential,
        leeway=timedelta(0),
        now=now,
    )


def _another_request_refreshed(
    latest: StoredCredential,
    baseline: StoredCredential,
) -> bool:
    return (
        latest.access_token != baseline.access_token
        or _aware(latest.updated_at) > _aware(baseline.updated_at)
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _refresh_lock_key(credential_id: str) -> str:
    digest = hashlib.sha256(credential_id.encode()).hexdigest()
    return f"agent:oauth-refresh:{digest}"
