from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Protocol


class RedisLeaseClient(Protocol):
    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> Any: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str,
    ) -> Any: ...


_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
""".strip()


@dataclass(slots=True)
class RedisLease:
    redis: RedisLeaseClient
    key: str
    owner: str
    released: bool = False

    @classmethod
    async def acquire(
        cls,
        redis: RedisLeaseClient,
        *,
        key: str,
        ttl_seconds: int,
    ) -> RedisLease | None:
        owner = secrets.token_urlsafe(24)
        acquired = await redis.set(key, owner, ex=ttl_seconds, nx=True)
        return cls(redis=redis, key=key, owner=owner) if acquired else None

    async def release(self) -> None:
        if self.released:
            return
        await self.redis.eval(_RELEASE_SCRIPT, 1, self.key, self.owner)
        self.released = True

    async def __aenter__(self) -> RedisLease:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.release()
