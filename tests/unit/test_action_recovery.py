from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.gateway import canonicalize_arguments
from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.recovery import ActionRecoveryWorker
from erpnext_agent.actions.repository import ActionNotFoundError
from erpnext_agent.auth.session_store import AgentSession
from erpnext_agent.auth.token_store import StoredCredential
from erpnext_agent.mcp.adapter import MCPEnvelope, MCPTransportError


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
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSessionContext:
        session = FakeSession()
        self.sessions.append(session)
        return FakeSessionContext(session)


class FakeRepository:
    def __init__(self, action: ActionRecord) -> None:
        self.action = action

    async def list_executing_ids(
        self,
        session: AsyncSession,
        *,
        limit: int,
    ) -> list[str]:
        del session, limit
        if self.action.status == ActionStatus.EXECUTING.value:
            return [self.action.action_id]
        return []

    async def get_executing(
        self,
        session: AsyncSession,
        *,
        action_id: str,
    ) -> ActionRecord:
        del session
        if (
            action_id != self.action.action_id
            or self.action.status != ActionStatus.EXECUTING.value
        ):
            raise ActionNotFoundError(action_id)
        return self.action


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


class FakeSessionStore:
    def __init__(self, credential: StoredCredential) -> None:
        self.deleted: list[str] = []
        self.session = AgentSession(
            session_id="agent-session-1",
            credential_id=credential.credential_id,
            binding_id=credential.binding_id,
            site=credential.site,
            user_id=credential.user_id,
            csrf_token="csrf-token",  # noqa: S106
            created_at=datetime.now(UTC).isoformat(),
        )

    async def get(self, session_id: str) -> AgentSession | None:
        return self.session if session_id == self.session.session_id else None

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)


class FakeTokenStore:
    def __init__(self, credential: StoredCredential) -> None:
        self.credential = credential

    async def get(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> StoredCredential:
        del session
        assert credential_id == self.credential.credential_id
        return self.credential


class FakeRefreshService:
    def __init__(self, credential: StoredCredential) -> None:
        self.credential = credential

    async def get_valid(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> StoredCredential:
        del session
        assert credential_id == self.credential.credential_id
        return self.credential

    async def refresh_after_auth_failure(
        self,
        session: AsyncSession,
        failed: StoredCredential,
    ) -> StoredCredential:
        del session
        assert failed == self.credential
        return self.credential


class FakeAdapter:
    def __init__(
        self,
        *,
        current_user: str = "user@example.com",
        write_error: Exception | None = None,
    ) -> None:
        self.current_user = current_user
        self.write_error = write_error
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
            return MCPEnvelope(data={"user": self.current_user}, meta={})
        if name == "erpnext_create_draft":
            if self.write_error is not None:
                raise self.write_error
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
                    "modified": "2026-08-12 12:00:00",
                },
                meta={"trace_id": "read-trace"},
            )
        raise AssertionError(f"Unexpected tool call: {name}")


def executing_action() -> ActionRecord:
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
    canonical, digest = canonicalize_arguments(arguments)
    now = datetime.now(UTC)
    return ActionRecord(
        action_id="action-recovery-1",
        session_id="agent-session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        tool_name="erpnext_create_draft",
        canonical_arguments=canonical,
        arguments_sha256=digest,
        preview={"title": "创建 Material Request 草稿"},
        source_versions={},
        idempotency_key="original-idempotency-key",
        status=ActionStatus.EXECUTING.value,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        decided_by="user@example.com",
        decided_at=now,
    )


def stored_credential() -> StoredCredential:
    return StoredCredential(
        credential_id="credential-1",
        binding_id="00000000-0000-0000-0000-000000000030",
        site="dev.localhost",
        oauth_subject="user@example.com",
        user_id="user@example.com",
        access_token="inert-access-token",  # noqa: S106
        refresh_token="inert-refresh-token",  # noqa: S106
        scope="all openid",
        expires_at=None,
        updated_at=datetime.now(UTC),
        revoked_at=None,
    )


def build_worker(
    action: ActionRecord,
    adapter: FakeAdapter,
) -> tuple[ActionRecoveryWorker, FakeRedis, FakeSessionStore]:
    credential = stored_credential()
    redis = FakeRedis()
    session_store = FakeSessionStore(credential)
    worker = ActionRecoveryWorker(
        enabled=True,
        session_factory=cast(Any, FakeSessionFactory()),
        redis=cast(Any, redis),
        repository=cast(Any, FakeRepository(action)),
        token_store=cast(Any, FakeTokenStore(credential)),
        refresh_service=cast(Any, FakeRefreshService(credential)),
        session_store=cast(Any, session_store),
        adapter=cast(Any, adapter),
        poll_seconds=15,
        retry_seconds=60,
        batch_size=20,
        execution_lock_ttl_seconds=300,
    )
    return worker, redis, session_store


@pytest.mark.asyncio
async def test_recovery_reuses_original_key_and_verifies_readback() -> None:
    action = executing_action()
    adapter = FakeAdapter()
    worker, _, _ = build_worker(action, adapter)

    report = await worker.run_once()

    assert report.scanned == 1
    assert report.recovered == 1
    assert action.status == ActionStatus.SUCCEEDED.value
    assert action.result_reference == {
        "doctype": "Material Request",
        "name": "MAT-MR-0001",
        "docstatus": 0,
        "modified": "2026-08-12 12:00:00",
    }
    assert [name for name, _ in adapter.calls] == [
        "erpnext_get_current_user",
        "erpnext_create_draft",
        "erpnext_get_doc",
    ]
    assert adapter.calls[1][1]["idempotency_key"] == "original-idempotency-key"


@pytest.mark.asyncio
async def test_uncertain_recovery_is_rate_limited_without_changing_key() -> None:
    action = executing_action()
    adapter = FakeAdapter(
        write_error=MCPTransportError("connection lost", code="MCP_UNAVAILABLE"),
    )
    worker, _, _ = build_worker(action, adapter)

    first = await worker.run_once()
    second = await worker.run_once()

    assert first.deferred == 1
    assert second.deferred == 1
    assert action.status == ActionStatus.EXECUTING.value
    assert action.failure_code == "MCP_UNAVAILABLE"
    write_calls = [call for call in adapter.calls if call[0] == "erpnext_create_draft"]
    assert len(write_calls) == 1
    assert write_calls[0][1]["idempotency_key"] == "original-idempotency-key"


@pytest.mark.asyncio
async def test_recovery_rejects_mcp_identity_mismatch_before_write() -> None:
    action = executing_action()
    adapter = FakeAdapter(current_user="attacker@example.com")
    worker, _, session_store = build_worker(action, adapter)

    report = await worker.run_once()

    assert report.errors == 1
    assert action.status == ActionStatus.EXECUTING.value
    assert action.failure_code == "IDENTITY_MISMATCH"
    assert session_store.deleted == [action.session_id]
    assert [name for name, _ in adapter.calls] == ["erpnext_get_current_user"]
