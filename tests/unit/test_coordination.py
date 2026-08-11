from typing import cast

import pytest

from erpnext_agent.coordination import RedisLease, RedisLeaseClient


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


@pytest.mark.asyncio
async def test_lease_is_single_flight_and_owner_safe() -> None:
    redis = FakeRedis()
    client = cast(RedisLeaseClient, redis)
    first = await RedisLease.acquire(client, key="lock", ttl_seconds=30)
    assert first is not None
    assert await RedisLease.acquire(client, key="lock", ttl_seconds=30) is None

    redis.values["lock"] = "new-owner-after-expiry"
    await first.release()
    assert redis.values["lock"] == "new-owner-after-expiry"
