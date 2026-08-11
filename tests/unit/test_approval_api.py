from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.repository import ActionNotFoundError
from erpnext_agent.api import approvals
from erpnext_agent.auth.session_store import AgentSession
from erpnext_agent.auth.token_store import StoredCredential
from erpnext_agent.mcp.adapter import MCPEnvelope


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1


class OwnedActionRepository:
    def __init__(self, record: ActionRecord) -> None:
        self.record = record

    async def get_for_user(
        self,
        session: AsyncSession,
        *,
        action_id: str,
        site: str,
        user_id: str,
        for_update: bool = False,
    ) -> ActionRecord:
        del session, for_update
        if (
            action_id != self.record.action_id
            or site != self.record.site
            or user_id != self.record.requested_by
        ):
            raise ActionNotFoundError(action_id)
        return self.record

    async def claim_execution(self, session: AsyncSession, action_id: str) -> bool:
        del session
        if action_id != self.record.action_id or self.record.status != ActionStatus.APPROVED:
            return False
        self.record.status = ActionStatus.EXECUTING.value
        return True

    @staticmethod
    def expire_if_needed(record: ActionRecord) -> bool:
        del record
        return False


class FakeRefreshService:
    def __init__(self, user_id: str) -> None:
        now = datetime.now(UTC)
        self.credential = StoredCredential(
            credential_id="credential-1",
            site="dev.localhost",
            oauth_subject=user_id,
            user_id=user_id,
            access_token="inert-access-token",  # noqa: S106
            refresh_token="inert-refresh-token",  # noqa: S106
            scope="all openid",
            expires_at=None,
            updated_at=now,
            revoked_at=None,
        )

    async def get_valid(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> StoredCredential:
        del session
        assert credential_id == self.credential.credential_id
        return self.credential


class FakeSessionStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)


class FakeRedis:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
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
        if self.busy or (nx and name in self.values):
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


class FakeAdapter:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope:
        del access_token, discover_first
        self.calls.append((name, arguments))
        if name == "erpnext_get_current_user":
            return MCPEnvelope(data={"user": self.user_id}, meta={})
        if name == "erpnext_create_draft":
            return MCPEnvelope(
                data={"doctype": "Material Request", "name": "MAT-MR-0001"},
                meta={"trace_id": "write-trace"},
            )
        if name == "erpnext_get_doc":
            return MCPEnvelope(
                data={
                    "doctype": "Material Request",
                    "name": "MAT-MR-0001",
                    "docstatus": 0,
                    "modified": "2026-08-12 08:00:00",
                },
                meta={},
            )
        raise AssertionError(f"Unexpected tool call: {name}")


def approved_action(user_id: str = "user@example.com") -> ActionRecord:
    now = datetime.now(UTC)
    arguments = {
        "doctype": "Material Request",
        "payload": {
            "material_request_type": "Purchase",
            "company": "Example Company",
            "transaction_date": "2026-08-12",
            "schedule_date": "2026-08-13",
            "items": [{"item_code": "ITEM-0001", "qty": 1}],
        },
    }
    from erpnext_agent.actions.gateway import canonicalize_arguments

    canonical, digest = canonicalize_arguments(arguments)
    return ActionRecord(
        action_id="action-1",
        session_id="session-1",
        site="dev.localhost",
        requested_by=user_id,
        tool_name="erpnext_create_draft",
        canonical_arguments=canonical,
        arguments_sha256=digest,
        preview={"title": "创建 Material Request 草稿"},
        source_versions={},
        idempotency_key="fixed-idempotency-key",
        status=ActionStatus.APPROVED.value,
        created_at=now,
        expires_at=now.replace(year=now.year + 1),
        decided_by=user_id,
        decided_at=now,
    )


def request_with_runtime(
    user_id: str,
    *,
    execution_busy: bool = False,
) -> tuple[Request, FakeAdapter]:
    adapter = FakeAdapter(user_id)
    state = SimpleNamespace(
        token_refresh_service=FakeRefreshService(user_id),
        session_store=FakeSessionStore(),
        mcp_adapter=adapter,
        redis=FakeRedis(busy=execution_busy),
        settings=SimpleNamespace(action_execution_lock_ttl_seconds=300),
    )
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
    return request, adapter


def agent_session(user_id: str = "user@example.com") -> AgentSession:
    return AgentSession(
        session_id="session-1",
        credential_id="credential-1",
        site="dev.localhost",
        user_id=user_id,
        csrf_token="csrf-token",  # noqa: S106
        created_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_execute_api_uses_current_identity_and_returns_verified_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = approved_action()
    repository = OwnedActionRepository(record)
    monkeypatch.setattr(approvals, "ActionRepository", lambda: repository)
    request, adapter = request_with_runtime(record.requested_by)

    view = await approvals.execute_action(
        record.action_id,
        request,
        agent_session(record.requested_by),
        cast(AsyncSession, FakeDB()),
    )

    assert view.status == ActionStatus.SUCCEEDED.value
    assert view.result_reference == {
        "doctype": "Material Request",
        "name": "MAT-MR-0001",
        "docstatus": 0,
        "modified": "2026-08-12 08:00:00",
    }
    assert [name for name, _ in adapter.calls] == [
        "erpnext_get_current_user",
        "erpnext_create_draft",
        "erpnext_get_doc",
    ]
    assert adapter.calls[1][1]["idempotency_key"] == "fixed-idempotency-key"


@pytest.mark.asyncio
async def test_execute_api_hides_another_users_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = approved_action(user_id="owner@example.com")
    repository = OwnedActionRepository(record)
    monkeypatch.setattr(approvals, "ActionRepository", lambda: repository)
    request, adapter = request_with_runtime("attacker@example.com")

    with pytest.raises(HTTPException) as caught:
        await approvals.execute_action(
            record.action_id,
            request,
            agent_session("attacker@example.com"),
            cast(AsyncSession, FakeDB()),
        )

    assert caught.value.status_code == 404
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_execute_api_reconciles_existing_executing_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = approved_action()
    record.status = ActionStatus.EXECUTING.value
    repository = OwnedActionRepository(record)
    monkeypatch.setattr(approvals, "ActionRepository", lambda: repository)
    request, adapter = request_with_runtime(record.requested_by)

    view = await approvals.execute_action(
        record.action_id,
        request,
        agent_session(record.requested_by),
        cast(AsyncSession, FakeDB()),
    )

    assert view.status == ActionStatus.SUCCEEDED.value
    assert [name for name, _ in adapter.calls] == [
        "erpnext_get_current_user",
        "erpnext_create_draft",
        "erpnext_get_doc",
    ]


@pytest.mark.asyncio
async def test_execute_api_returns_busy_without_calling_mcp_when_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = approved_action()
    repository = OwnedActionRepository(record)
    monkeypatch.setattr(approvals, "ActionRepository", lambda: repository)
    request, adapter = request_with_runtime(record.requested_by, execution_busy=True)

    with pytest.raises(HTTPException) as caught:
        await approvals.execute_action(
            record.action_id,
            request,
            agent_session(record.requested_by),
            cast(AsyncSession, FakeDB()),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "ACTION_EXECUTION_BUSY"
    assert adapter.calls == []
