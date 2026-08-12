from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erpnext_agent.conversations.repository import ConversationRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConversationRetentionPolicy:
    enabled: bool
    retention_days: int
    deleted_retention_days: int
    empty_retention_hours: int
    sweep_seconds: int
    batch_size: int


@dataclass(frozen=True, slots=True)
class ConversationRetentionReport:
    purged: int
    completed_at: datetime


class ConversationRetentionWorker:
    """Purge expired chat content while leaving Action audit records untouched."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        repository: ConversationRepository,
        policy: ConversationRetentionPolicy,
    ) -> None:
        self.enabled = policy.enabled
        self._session_factory = session_factory
        self._repository = repository
        self._policy = policy
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_report: ConversationRetentionReport | None = None

    @property
    def running(self) -> bool:
        return bool(self._task is not None and not self._task.done())

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(),
            name="conversation-retention-worker",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await self._task
        self._task = None

    async def run_once(self, *, now: datetime | None = None) -> ConversationRetentionReport:
        completed_at = now or datetime.now(UTC)
        async with self._session_factory() as session:
            purged = await self._repository.purge_expired(
                session,
                now=completed_at,
                retention_days=self._policy.retention_days,
                deleted_retention_days=self._policy.deleted_retention_days,
                empty_retention_hours=self._policy.empty_retention_hours,
                limit=self._policy.batch_size,
            )
            await session.commit()
        report = ConversationRetentionReport(
            purged=purged,
            completed_at=completed_at,
        )
        self.last_report = report
        if purged:
            logger.info("Expired conversations purged", extra={"count": purged})
        return report

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Conversation retention sweep failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._policy.sweep_seconds,
                )
            except TimeoutError:
                continue
