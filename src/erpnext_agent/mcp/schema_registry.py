from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class SchemaCacheScope:
    site: str
    user_id: str
    mcp_version: str
    policy_version: str

    def prefix(self) -> str:
        raw = json.dumps(
            [self.site, self.user_id, self.mcp_version, self.policy_version],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()


class SchemaRegistry:
    def __init__(self, redis: Redis, ttl_seconds: int = 300) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(self, scope: SchemaCacheScope, name: str) -> dict[str, Any] | None:
        value = await self._redis.get(f"schema:{scope.prefix()}:{name}")
        return json.loads(value) if value is not None else None

    async def put(self, scope: SchemaCacheScope, name: str, value: dict[str, Any]) -> None:
        await self._redis.setex(
            f"schema:{scope.prefix()}:{name}",
            self._ttl_seconds,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

