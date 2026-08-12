from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class AgentSession:
    session_id: str
    credential_id: str
    binding_id: str
    site: str
    user_id: str
    csrf_token: str
    created_at: str


@dataclass(frozen=True, slots=True)
class OAuthState:
    verifier: str
    nonce: str
    attempt_hash: str
    return_to: str
    binding_id: str


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int, hmac_secret: str) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._hmac_secret = hmac_secret.encode()

    async def create(
        self,
        *,
        credential_id: str,
        binding_id: str,
        site: str,
        user_id: str,
    ) -> AgentSession:
        raw_id = secrets.token_urlsafe(32)
        session = AgentSession(
            session_id=raw_id,
            credential_id=credential_id,
            binding_id=binding_id,
            site=site,
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=datetime.now(UTC).isoformat(),
        )
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.setex(self._key(raw_id), self._ttl_seconds, json.dumps(asdict(session)))
            pipeline.sadd(self._binding_key(binding_id), raw_id)
            pipeline.expire(self._binding_key(binding_id), self._ttl_seconds)
            pipeline.sadd(self._credential_key(credential_id), raw_id)
            pipeline.expire(self._credential_key(credential_id), self._ttl_seconds)
            await pipeline.execute()
        return session

    async def get(self, raw_id: str) -> AgentSession | None:
        value = await self._redis.get(self._key(raw_id))
        if value is None:
            return None
        payload = json.loads(value)
        if "binding_id" not in payload:
            await self._redis.delete(self._key(raw_id))
            return None
        await self._redis.expire(self._key(raw_id), self._ttl_seconds)
        session = AgentSession(**payload)
        await self._touch_indexes(session)
        return session

    async def delete(self, raw_id: str) -> None:
        value = await self._redis.get(self._key(raw_id))
        if value is None:
            return
        payload = json.loads(value)
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.delete(self._key(raw_id))
            if payload.get("binding_id"):
                pipeline.srem(self._binding_key(str(payload["binding_id"])), raw_id)
            if payload.get("credential_id"):
                pipeline.srem(self._credential_key(str(payload["credential_id"])), raw_id)
            await pipeline.execute()

    async def delete_by_binding(self, binding_id: str) -> list[str]:
        raw_ids = list(
            await self._redis.smembers(self._binding_key(binding_id))  # type: ignore[misc]
        )
        for raw_id in raw_ids:
            await self.delete(raw_id)
        await self._redis.delete(self._binding_key(binding_id))
        return raw_ids

    async def delete_by_credential(self, credential_id: str) -> list[str]:
        raw_ids = list(
            await self._redis.smembers(self._credential_key(credential_id))  # type: ignore[misc]
        )
        for raw_id in raw_ids:
            await self.delete(raw_id)
        await self._redis.delete(self._credential_key(credential_id))
        return raw_ids

    async def _touch_indexes(self, session: AgentSession) -> None:
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.expire(self._binding_key(session.binding_id), self._ttl_seconds)
            pipeline.expire(self._credential_key(session.credential_id), self._ttl_seconds)
            await pipeline.execute()

    def _key(self, raw_id: str) -> str:
        digest = hmac.new(self._hmac_secret, raw_id.encode(), hashlib.sha256).hexdigest()
        return f"agent:session:{digest}"

    @staticmethod
    def _binding_key(binding_id: str) -> str:
        return f"agent:binding-sessions:{binding_id}"

    @staticmethod
    def _credential_key(credential_id: str) -> str:
        return f"agent:credential-sessions:{credential_id}"


class OAuthStateStore:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def put(self, state: str, value: OAuthState) -> None:
        await self._redis.setex(
            f"oauth:state:{state}",
            self._ttl_seconds,
            json.dumps(asdict(value)),
        )

    async def consume(self, state: str) -> OAuthState | None:
        value = await self._redis.getdel(f"oauth:state:{state}")
        if value is None:
            return None
        return OAuthState(**json.loads(value))


def hash_attempt_cookie(raw_value: str) -> str:
    return hashlib.sha256(raw_value.encode()).hexdigest()
