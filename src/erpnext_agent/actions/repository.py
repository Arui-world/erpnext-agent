from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.models import ActionRecord, ActionStatus


class ActionNotFoundError(LookupError):
    pass


class ActionStateError(RuntimeError):
    pass


class ActionRepository:
    async def add(self, session: AsyncSession, record: ActionRecord) -> ActionRecord:
        session.add(record)
        await session.flush()
        return record

    async def get_for_user(
        self,
        session: AsyncSession,
        *,
        action_id: str,
        site: str,
        user_id: str,
        for_update: bool = False,
    ) -> ActionRecord:
        query = select(ActionRecord).where(
            ActionRecord.action_id == action_id,
            ActionRecord.site == site,
            ActionRecord.requested_by == user_id,
        )
        if for_update:
            query = query.with_for_update()
        record = await session.scalar(query)
        if record is None:
            raise ActionNotFoundError(action_id)
        return record

    async def claim_execution(self, session: AsyncSession, action_id: str) -> bool:
        now = datetime.now(UTC)
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(ActionRecord)
                .where(
                    ActionRecord.action_id == action_id,
                    ActionRecord.status == ActionStatus.APPROVED.value,
                    ActionRecord.expires_at > now,
                )
                .values(status=ActionStatus.EXECUTING.value)
            )
        )
        return bool(result.rowcount == 1)

    @staticmethod
    def expire_if_needed(record: ActionRecord, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        expires_at = record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if record.status in {
            ActionStatus.PENDING.value,
            ActionStatus.APPROVED.value,
        } and expires_at <= current:
            record.status = ActionStatus.EXPIRED.value
            return True
        return False
