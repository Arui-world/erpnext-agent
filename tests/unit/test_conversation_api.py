from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.api import chat
from erpnext_agent.auth.session_store import AgentSession
from erpnext_agent.conversations.repository import ConversationNotFoundError

CONVERSATION_ID = UUID("00000000-0000-0000-0000-000000000123")


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeLifecycleRepository:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.rename_scope: dict[str, object] | None = None
        self.delete_scope: dict[str, object] | None = None

    async def rename_owned(
        self,
        session: AsyncSession,
        **scope: object,
    ) -> SimpleNamespace:
        del session
        self.rename_scope = scope
        if not self.exists:
            raise ConversationNotFoundError(str(CONVERSATION_ID))
        return SimpleNamespace(
            conversation_id=str(CONVERSATION_ID),
            title=scope["title"],
            updated_at=datetime.now(UTC),
        )

    async def soft_delete_owned(
        self,
        session: AsyncSession,
        **scope: object,
    ) -> None:
        del session
        self.delete_scope = scope
        if not self.exists:
            raise ConversationNotFoundError(str(CONVERSATION_ID))


def agent_session(user_id: str = "owner@example.com") -> AgentSession:
    return AgentSession(
        session_id="session-1",
        credential_id="credential-1",
        binding_id="00000000-0000-0000-0000-000000000999",
        site="dev.localhost",
        user_id=user_id,
        csrf_token="csrf-token",  # noqa: S106
        created_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_rename_conversation_uses_session_identity_and_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeLifecycleRepository()
    monkeypatch.setattr(chat, "ConversationRepository", lambda: repository)
    db = FakeDB()
    payload = chat.RenameConversationRequest(mode="agent", title="  库存\n分析 ")

    response = await chat.rename_conversation(
        CONVERSATION_ID,
        payload,
        agent_session(),
        cast(AsyncSession, db),
    )

    assert response.title == "库存 分析"
    assert repository.rename_scope == {
        "conversation_id": str(CONVERSATION_ID),
        "site": "dev.localhost",
        "user_id": "owner@example.com",
        "mode": "agent",
        "title": "库存 分析",
    }
    assert db.commits == 1


@pytest.mark.asyncio
async def test_cross_user_rename_fails_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeLifecycleRepository(exists=False)
    monkeypatch.setattr(chat, "ConversationRepository", lambda: repository)
    db = FakeDB()

    with pytest.raises(HTTPException) as exc_info:
        await chat.rename_conversation(
            CONVERSATION_ID,
            chat.RenameConversationRequest(mode="model", title="越权重命名"),
            agent_session("other@example.com"),
            cast(AsyncSession, db),
        )

    assert exc_info.value.status_code == 404
    assert db.commits == 0


@pytest.mark.asyncio
async def test_delete_conversation_uses_session_identity_and_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeLifecycleRepository()
    monkeypatch.setattr(chat, "ConversationRepository", lambda: repository)
    db = FakeDB()

    response = await chat.delete_conversation(
        CONVERSATION_ID,
        agent_session(),
        cast(AsyncSession, db),
        mode="model",
    )

    assert response.status_code == 204
    assert repository.delete_scope == {
        "conversation_id": str(CONVERSATION_ID),
        "site": "dev.localhost",
        "user_id": "owner@example.com",
        "mode": "model",
    }
    assert db.commits == 1


@pytest.mark.asyncio
async def test_cross_user_delete_fails_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeLifecycleRepository(exists=False)
    monkeypatch.setattr(chat, "ConversationRepository", lambda: repository)
    db = FakeDB()

    with pytest.raises(HTTPException) as exc_info:
        await chat.delete_conversation(
            CONVERSATION_ID,
            agent_session("other@example.com"),
            cast(AsyncSession, db),
            mode="agent",
        )

    assert exc_info.value.status_code == 404
    assert db.commits == 0
