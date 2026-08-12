from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erpnext_agent.conversations.repository import ConversationRepository
from erpnext_agent.conversations.retention import (
    ConversationRetentionPolicy,
    ConversationRetentionWorker,
)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return cast(AsyncSession, self.session)

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self.session)


class FakeRepository:
    def __init__(self) -> None:
        self.arguments: dict[str, Any] | None = None

    async def purge_expired(
        self,
        session: AsyncSession,
        **arguments: Any,
    ) -> int:
        del session
        self.arguments = arguments
        return 3


def policy(*, enabled: bool = True) -> ConversationRetentionPolicy:
    return ConversationRetentionPolicy(
        enabled=enabled,
        retention_days=180,
        deleted_retention_days=7,
        empty_retention_hours=24,
        sweep_seconds=3600,
        batch_size=100,
    )


@pytest.mark.asyncio
async def test_retention_worker_passes_bounded_policy_and_commits() -> None:
    factory = FakeSessionFactory()
    repository = FakeRepository()
    worker = ConversationRetentionWorker(
        session_factory=cast(async_sessionmaker[AsyncSession], factory),
        repository=cast(ConversationRepository, repository),
        policy=policy(),
    )
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

    report = await worker.run_once(now=now)

    assert report.purged == 3
    assert report.completed_at == now
    assert repository.arguments == {
        "now": now,
        "retention_days": 180,
        "deleted_retention_days": 7,
        "empty_retention_hours": 24,
        "limit": 100,
    }
    assert factory.session.commits == 1
    assert worker.last_report == report


@pytest.mark.asyncio
async def test_disabled_retention_worker_does_not_start() -> None:
    worker = ConversationRetentionWorker(
        session_factory=cast(async_sessionmaker[AsyncSession], FakeSessionFactory()),
        repository=cast(ConversationRepository, FakeRepository()),
        policy=policy(enabled=False),
    )

    worker.start()
    await worker.stop()

    assert worker.running is False
    assert worker.last_report is None
