from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.repository import ActionRepository, ActionStateError
from erpnext_agent.mcp.policy import WRITE_TOOLS


def canonicalize_arguments(arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError("Action arguments exceed the 64 KiB limit")
    normalized = json.loads(encoded)
    return normalized, hashlib.sha256(encoded.encode()).hexdigest()


class ActionGateway:
    def __init__(self, repository: ActionRepository, ttl_seconds: int) -> None:
        self._repository = repository
        self._ttl_seconds = ttl_seconds

    async def create_pending(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        site: str,
        requested_by: str,
        tool_name: str,
        arguments: dict[str, Any],
        preview: dict[str, Any],
        source_versions: dict[str, Any] | None = None,
    ) -> ActionRecord:
        if tool_name not in WRITE_TOOLS:
            raise ValueError("Only approved draft-write tools can create an Action")
        canonical, digest = canonicalize_arguments(arguments)
        now = datetime.now(UTC)
        record = ActionRecord(
            action_id=str(uuid.uuid4()),
            session_id=session_id,
            site=site,
            requested_by=requested_by,
            tool_name=tool_name,
            canonical_arguments=canonical,
            arguments_sha256=digest,
            preview=preview,
            source_versions=source_versions or {},
            idempotency_key=secrets.token_urlsafe(24),
            status=ActionStatus.PENDING.value,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        return await self._repository.add(session, record)

    async def decide(
        self,
        session: AsyncSession,
        *,
        action_id: str,
        site: str,
        user_id: str,
        session_id: str,
        decision: Literal["approve", "reject"],
    ) -> ActionRecord:
        record = await self._repository.get_for_user(
            session,
            action_id=action_id,
            site=site,
            user_id=user_id,
            session_id=session_id,
            for_update=True,
        )
        if self._repository.expire_if_needed(record):
            # Expiry is a business fact and must survive the HTTP 409 returned by
            # the route's surrounding transaction handler.
            await session.commit()
            raise ActionStateError("Action approval has expired")
        if record.status != ActionStatus.PENDING.value:
            raise ActionStateError(f"Action cannot be decided from status {record.status}")
        record.status = (
            ActionStatus.APPROVED.value if decision == "approve" else ActionStatus.REJECTED.value
        )
        record.decided_by = user_id
        record.decided_at = datetime.now(UTC)
        await session.flush()
        return record
