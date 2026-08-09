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


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int, hmac_secret: str) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._hmac_secret = hmac_secret.encode()

    async def create(self, *, credential_id: str, site: str, user_id: str) -> AgentSession:
        raw_id = secrets.token_urlsafe(32)
        session = AgentSession(
            session_id=raw_id,
            credential_id=credential_id,
            site=site,
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=datetime.now(UTC).isoformat(),
        )
        await self._redis.setex(self._key(raw_id), self._ttl_seconds, json.dumps(asdict(session)))
        return session

    async def get(self, raw_id: str) -> AgentSession | None:
        value = await self._redis.get(self._key(raw_id))
        if value is None:
            return None
        payload = json.loads(value)
        await self._redis.expire(self._key(raw_id), self._ttl_seconds)
        return AgentSession(**payload)

    async def delete(self, raw_id: str) -> None:
        await self._redis.delete(self._key(raw_id))

    def _key(self, raw_id: str) -> str:
        digest = hmac.new(self._hmac_secret, raw_id.encode(), hashlib.sha256).hexdigest()
        return f"agent:session:{digest}"


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
