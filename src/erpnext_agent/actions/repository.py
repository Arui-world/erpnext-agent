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
        session_id: str,
        for_update: bool = False,
    ) -> ActionRecord:
        query = select(ActionRecord).where(
            ActionRecord.action_id == action_id,
            ActionRecord.site == site,
            ActionRecord.requested_by == user_id,
            ActionRecord.session_id == session_id,
        ).execution_options(populate_existing=True)
        if for_update:
            query = query.with_for_update()
        record = await session.scalar(query)
        if record is None:
            raise ActionNotFoundError(action_id)
        return record

    async def get_executing(
        self,
        session: AsyncSession,
        *,
        action_id: str,
    ) -> ActionRecord:
        record = await session.scalar(
            select(ActionRecord).where(
                ActionRecord.action_id == action_id,
                ActionRecord.status == ActionStatus.EXECUTING.value,
            )
        )
        if record is None:
            raise ActionNotFoundError(action_id)
        return record

    async def list_executing_ids(
        self,
        session: AsyncSession,
        *,
        limit: int,
    ) -> list[str]:
        result = await session.scalars(
            select(ActionRecord.action_id)
            .where(ActionRecord.status == ActionStatus.EXECUTING.value)
            .order_by(ActionRecord.created_at, ActionRecord.action_id)
            .limit(limit)
        )
        return list(result)

    async def list_for_conversation(
        self,
        session: AsyncSession,
        *,
        site: str,
        user_id: str,
        session_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[ActionRecord]:
        result = await session.scalars(
            select(ActionRecord)
            .where(
                ActionRecord.site == site,
                ActionRecord.requested_by == user_id,
                ActionRecord.session_id == session_id,
                ActionRecord.preview["conversation_id"].as_string()
                == conversation_id,
            )
            .order_by(ActionRecord.created_at.desc(), ActionRecord.action_id.desc())
            .limit(limit)
        )
        # The database query keeps the newest bounded set. The API returns it in
        # chronological order so restored cards follow their original chat turns.
        return list(reversed(list(result)))

    async def expire_for_sessions(
        self,
        session: AsyncSession,
        *,
        session_ids: list[str],
        failure_code: str = "ERP_AUTHORIZATION_REVOKED",
    ) -> int:
        if not session_ids:
            return 0
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(ActionRecord)
                .where(
                    ActionRecord.session_id.in_(session_ids),
                    ActionRecord.status.in_(
                        [ActionStatus.PENDING.value, ActionStatus.APPROVED.value]
                    ),
                )
                .values(
                    status=ActionStatus.EXPIRED.value,
                    failure_code=failure_code,
                    failure_message="ERPNext browser authorization was revoked",
                )
            ),
        )
        return int(result.rowcount or 0)

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
