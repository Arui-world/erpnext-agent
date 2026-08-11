from __future__ import annotations

import hashlib
import secrets

from erpnext_agent.coordination import RedisLease, RedisLeaseClient


def action_execution_lock_key(action_id: str) -> str:
    digest = hashlib.sha256(action_id.encode()).hexdigest()
    return f"agent:action-execution:{digest}"


def action_recovery_cooldown_key(action_id: str) -> str:
    digest = hashlib.sha256(action_id.encode()).hexdigest()
    return f"agent:action-recovery:{digest}"


async def acquire_action_execution_lease(
    redis: RedisLeaseClient,
    *,
    action_id: str,
    ttl_seconds: int,
) -> RedisLease | None:
    """Serialize manual and background execution of one persisted Action."""

    return await RedisLease.acquire(
        redis,
        key=action_execution_lock_key(action_id),
        ttl_seconds=ttl_seconds,
    )


async def claim_action_recovery_attempt(
    redis: RedisLeaseClient,
    *,
    action_id: str,
    cooldown_seconds: int,
) -> bool:
    """Limit uncertain same-key retries across all service instances."""

    claimed = await redis.set(
        action_recovery_cooldown_key(action_id),
        secrets.token_urlsafe(24),
        ex=cooldown_seconds,
        nx=True,
    )
    return bool(claimed)
