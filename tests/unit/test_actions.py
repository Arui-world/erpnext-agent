from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.executor import ActionExecutor
from erpnext_agent.actions.gateway import ActionGateway, canonicalize_arguments
from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.proposal import (
    CREATE_DRAFT_TOOL,
    PROPOSE_DRAFT_TOOL,
    UPDATE_DRAFT_TOOL,
    ActionProposalError,
    ActionProposalService,
    ActionProposalTool,
    build_action_preview,
    validate_action_arguments,
)
from erpnext_agent.actions.repository import ActionRepository, ActionStateError
from erpnext_agent.mcp.adapter import (
    MCPBusinessError,
    MCPContractError,
    MCPEnvelope,
)

TEST_ACCESS_TOKEN = "inert-action-token"  # noqa: S105


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1


class FakeRepository:
    def __init__(self) -> None:
        self.records: list[ActionRecord] = []

    async def add(self, session: AsyncSession, record: ActionRecord) -> ActionRecord:
        del session
        self.records.append(record)
        return record

    async def claim_execution(self, session: AsyncSession, action_id: str) -> bool:
        del session
        record = next(item for item in self.records if item.action_id == action_id)
        if record.status != ActionStatus.APPROVED.value:
            return False
        record.status = ActionStatus.EXECUTING.value
        return True


class ProposalCaller:
    def __init__(self, *, modified: str = "2026-08-11 10:00:00") -> None:
        self.modified = modified
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
        if name == "erpnext_get_doctype_schema":
            return MCPEnvelope(
                data={
                    "fields": [
                        {"fieldname": field, "read_only": False}
                        for field in (
                            "material_request_type",
                            "company",
                            "transaction_date",
                            "schedule_date",
                            "set_warehouse",
                            "items",
                        )
                    ],
                },
                meta={},
            )
        if name == "erpnext_get_list":
            names = arguments["filters"]["name"][1]
            return MCPEnvelope(data={"rows": [{"name": value} for value in names]}, meta={})
        if name == "erpnext_get_doc":
            return MCPEnvelope(
                data={
                    "doctype": arguments["doctype"],
                    "name": arguments["name"],
                    "docstatus": 0,
                    "modified": self.modified,
                },
                meta={},
            )
        raise AssertionError(f"Unexpected tool call: {name}")


class ExecutorCaller:
    def __init__(
        self,
        *,
        write_error: Exception | None = None,
        readback_error: Exception | None = None,
    ) -> None:
        self.write_error = write_error
        self.readback_error = readback_error
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
        if name == CREATE_DRAFT_TOOL:
            if self.write_error:
                raise self.write_error
            return MCPEnvelope(
                data={"doctype": "Material Request", "name": "MAT-MR-0001"},
                meta={"trace_id": "write-trace"},
            )
        if name == "erpnext_get_doc":
            if self.readback_error:
                raise self.readback_error
            return MCPEnvelope(
                data={
                    "doctype": "Material Request",
                    "name": "MAT-MR-0001",
                    "docstatus": 0,
                    "modified": "2026-08-11 10:01:00",
                },
                meta={"trace_id": "read-trace"},
            )
        raise AssertionError(f"Unexpected tool call: {name}")


def material_request_arguments() -> dict[str, Any]:
    return {
        "doctype": "Material Request",
        "payload": {
            "material_request_type": "Purchase",
            "company": "Example Company",
            "transaction_date": "2026-08-11",
            "schedule_date": "2026-08-12",
            "set_warehouse": "Stores - EX",
            "items": [
                {
                    "item_code": "ITEM-0001",
                    "qty": 2,
                    "schedule_date": "2026-08-12",
                    "warehouse": "Stores - EX",
                },
            ],
        },
    }


def approved_action() -> ActionRecord:
    arguments = material_request_arguments()
    canonical, digest = canonicalize_arguments(arguments)
    now = datetime.now(UTC)
    return ActionRecord(
        action_id="action-1",
        session_id="session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        tool_name=CREATE_DRAFT_TOOL,
        canonical_arguments=canonical,
        arguments_sha256=digest,
        preview=build_action_preview(CREATE_DRAFT_TOOL, canonical),
        source_versions={},
        idempotency_key="fixed-idempotency-key",
        status=ActionStatus.APPROVED.value,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        decided_by="user@example.com",
        decided_at=now,
    )


def material_request_update_arguments(
    *,
    expected_modified: str = "2026-08-11 10:00:00",
) -> dict[str, Any]:
    return {
        "doctype": "Material Request",
        "name": "MAT-MR-0001",
        "payload": {"schedule_date": "2026-08-13"},
        "expected_modified": expected_modified,
    }


def test_action_argument_hash_is_order_independent() -> None:
    first, first_hash = canonicalize_arguments(
        {"doctype": "Sales Order", "payload": {"b": 2, "a": 1}}
    )
    second, second_hash = canonicalize_arguments(
        {"payload": {"a": 1, "b": 2}, "doctype": "Sales Order"}
    )
    assert first == second
    assert first_hash == second_hash


def test_action_argument_hash_changes_with_parameters() -> None:
    _, first_hash = canonicalize_arguments({"name": "SO-1"})
    _, second_hash = canonicalize_arguments({"name": "SO-2"})
    assert first_hash != second_hash


def test_create_proposal_validates_and_normalizes_payload() -> None:
    arguments = material_request_arguments()
    arguments["payload"]["company"] = "  Example Company  "

    normalized = validate_action_arguments(CREATE_DRAFT_TOOL, arguments)

    assert normalized["payload"]["company"] == "Example Company"
    assert normalized["payload"]["items"][0]["qty"] == 2
    assert "idempotency_key" not in normalized


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda args: args.update(idempotency_key="client-key"), "Unexpected action arguments"),
        (lambda args: args["payload"].update(owner="Administrator"), "not allowed"),
        (lambda args: args["payload"]["items"][0].update(qty=0), "positive number"),
        (lambda args: args["payload"]["items"][0].update(qty=float("nan")), "positive number"),
        (lambda args: args["payload"].pop("company"), "Missing required"),
    ],
)
def test_create_proposal_rejects_unsafe_or_incomplete_arguments(
    mutation: Any,
    message: str,
) -> None:
    arguments = material_request_arguments()
    mutation(arguments)

    with pytest.raises(ActionProposalError, match=message):
        validate_action_arguments(CREATE_DRAFT_TOOL, arguments)


@pytest.mark.asyncio
async def test_proposal_tool_validates_links_and_persists_pending_action() -> None:
    repository = FakeRepository()
    caller = ProposalCaller()
    db = FakeSession()
    tool = ActionProposalTool(
        service=ActionProposalService(
            ActionGateway(cast(Any, repository), ttl_seconds=900),
            caller,
        ),
        session=cast(AsyncSession, db),
        session_id="session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        access_token=TEST_ACCESS_TOKEN,
        conversation_id="conversation-1",
    )

    permission = await tool.check_permissions({}, PermissionContext())
    chunk = await tool.call(
        tool_name=CREATE_DRAFT_TOOL,
        arguments=material_request_arguments(),
    )

    assert tool.name == PROPOSE_DRAFT_TOOL
    assert permission.behavior == PermissionBehavior.ALLOW
    assert chunk.state == ToolResultState.SUCCESS
    assert isinstance(chunk.content[0], TextBlock)
    payload = json.loads(chunk.content[0].text)
    assert payload["action"]["status"] == ActionStatus.PENDING.value
    assert payload["action"]["preview"]["conversation_id"] == "conversation-1"
    assert repository.records == [tool.record]
    assert db.commits == 1
    assert {name for name, _ in caller.calls} == {
        "erpnext_get_doctype_schema",
        "erpnext_get_list",
    }
    assert CREATE_DRAFT_TOOL not in {name for name, _ in caller.calls}


@pytest.mark.asyncio
async def test_proposal_tool_allows_only_one_action_per_turn() -> None:
    repository = FakeRepository()
    tool = ActionProposalTool(
        service=ActionProposalService(
            ActionGateway(cast(Any, repository), ttl_seconds=900),
            ProposalCaller(),
        ),
        session=cast(AsyncSession, FakeSession()),
        session_id="session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        access_token=TEST_ACCESS_TOKEN,
        conversation_id="conversation-1",
    )
    await tool.call(tool_name=CREATE_DRAFT_TOOL, arguments=material_request_arguments())

    second = await tool.call(
        tool_name=CREATE_DRAFT_TOOL,
        arguments=material_request_arguments(),
    )

    assert second.state == ToolResultState.ERROR
    assert "ACTION_ALREADY_PROPOSED" in cast(TextBlock, second.content[0]).text
    assert len(repository.records) == 1


@pytest.mark.asyncio
async def test_update_proposal_binds_exact_modified_version() -> None:
    repository = FakeRepository()
    caller = ProposalCaller()
    db = FakeSession()
    service = ActionProposalService(
        ActionGateway(cast(Any, repository), ttl_seconds=900),
        caller,
    )

    record = await service.propose(
        cast(AsyncSession, db),
        session_id="session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        access_token=TEST_ACCESS_TOKEN,
        tool_name=UPDATE_DRAFT_TOOL,
        arguments=material_request_update_arguments(),
        conversation_id="conversation-1",
    )

    assert record.source_versions == {"modified": "2026-08-11 10:00:00"}
    assert record.canonical_arguments["expected_modified"] == "2026-08-11 10:00:00"
    assert ("erpnext_get_doc", {"doctype": "Material Request", "name": "MAT-MR-0001"}) in (
        caller.calls
    )


@pytest.mark.asyncio
async def test_update_proposal_rejects_stale_modified_version() -> None:
    repository = FakeRepository()
    service = ActionProposalService(
        ActionGateway(cast(Any, repository), ttl_seconds=900),
        ProposalCaller(),
    )

    with pytest.raises(ActionProposalError) as caught:
        await service.propose(
            cast(AsyncSession, FakeSession()),
            session_id="session-1",
            site="dev.localhost",
            requested_by="user@example.com",
            access_token=TEST_ACCESS_TOKEN,
            tool_name=UPDATE_DRAFT_TOOL,
            arguments=material_request_update_arguments(
                expected_modified="2026-08-11 09:00:00",
            ),
        )

    assert caught.value.code == "VERSION_CONFLICT"
    assert repository.records == []


@pytest.mark.asyncio
async def test_executor_injects_fixed_idempotency_key_and_verifies_readback() -> None:
    repository = FakeRepository()
    action = approved_action()
    repository.records.append(action)
    caller = ExecutorCaller()
    db = FakeSession()

    await ActionExecutor(cast(Any, repository), caller).execute(
        cast(AsyncSession, db),
        action=action,
        access_token=TEST_ACCESS_TOKEN,
    )

    assert action.status == ActionStatus.SUCCEEDED.value
    assert action.result_reference == {
        "doctype": "Material Request",
        "name": "MAT-MR-0001",
        "docstatus": 0,
        "modified": "2026-08-11 10:01:00",
    }
    assert action.executed_at is not None
    assert caller.calls[0][1]["idempotency_key"] == "fixed-idempotency-key"
    assert caller.calls[1] == (
        "erpnext_get_doc",
        {"doctype": "Material Request", "name": "MAT-MR-0001"},
    )
    assert db.commits == 2


@pytest.mark.asyncio
async def test_executor_marks_proven_business_rejection_failed() -> None:
    repository = FakeRepository()
    action = approved_action()
    repository.records.append(action)
    caller = ExecutorCaller(
        write_error=MCPBusinessError(
            "Version conflict",
            code="VERSION_CONFLICT",
            trace_id="conflict-trace",
        ),
    )

    with pytest.raises(MCPBusinessError):
        await ActionExecutor(cast(Any, repository), caller).execute(
            cast(AsyncSession, FakeSession()),
            action=action,
            access_token=TEST_ACCESS_TOKEN,
        )

    assert action.status == ActionStatus.FAILED.value
    assert action.failure_code == "VERSION_CONFLICT"
    assert action.mcp_trace_id == "conflict-trace"


@pytest.mark.asyncio
async def test_executor_keeps_idempotency_in_progress_executing() -> None:
    repository = FakeRepository()
    action = approved_action()
    repository.records.append(action)
    caller = ExecutorCaller(
        write_error=MCPBusinessError(
            "Write is still in progress",
            code="IDEMPOTENCY_IN_PROGRESS",
            trace_id="in-progress-trace",
        ),
    )

    with pytest.raises(MCPBusinessError):
        await ActionExecutor(cast(Any, repository), caller).execute(
            cast(AsyncSession, FakeSession()),
            action=action,
            access_token=TEST_ACCESS_TOKEN,
        )

    assert action.status == ActionStatus.EXECUTING.value
    assert action.failure_code == "IDEMPOTENCY_IN_PROGRESS"


@pytest.mark.asyncio
async def test_executor_reconciles_executing_action_with_original_key() -> None:
    repository = FakeRepository()
    action = approved_action()
    action.status = ActionStatus.EXECUTING.value
    repository.records.append(action)
    caller = ExecutorCaller()

    await ActionExecutor(cast(Any, repository), caller).reconcile(
        cast(AsyncSession, FakeSession()),
        action=action,
        access_token=TEST_ACCESS_TOKEN,
    )

    assert action.status == ActionStatus.SUCCEEDED.value
    assert caller.calls[0][1]["idempotency_key"] == "fixed-idempotency-key"


@pytest.mark.asyncio
async def test_executor_keeps_uncertain_readback_failure_executing() -> None:
    repository = FakeRepository()
    action = approved_action()
    repository.records.append(action)
    caller = ExecutorCaller(
        readback_error=MCPContractError("Readback unavailable", code="READBACK_FAILED"),
    )

    with pytest.raises(MCPContractError):
        await ActionExecutor(cast(Any, repository), caller).execute(
            cast(AsyncSession, FakeSession()),
            action=action,
            access_token=TEST_ACCESS_TOKEN,
        )

    assert action.status == ActionStatus.EXECUTING.value
    assert action.failure_code == "READBACK_FAILED"
    assert caller.calls[0][0] == CREATE_DRAFT_TOOL


@pytest.mark.asyncio
async def test_executor_rejects_tampered_arguments_before_write() -> None:
    repository = FakeRepository()
    action = approved_action()
    repository.records.append(action)
    action.canonical_arguments["payload"]["company"] = "Tampered Company"
    caller = ExecutorCaller()

    with pytest.raises(ActionStateError, match="no longer match"):
        await ActionExecutor(cast(Any, repository), caller).execute(
            cast(AsyncSession, FakeSession()),
            action=action,
            access_token=TEST_ACCESS_TOKEN,
        )

    assert caller.calls == []
    assert action.status == ActionStatus.APPROVED.value


def test_approved_action_expires_before_delayed_execution() -> None:
    action = approved_action()
    action.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert ActionRepository.expire_if_needed(action) is True
    assert action.status == ActionStatus.EXPIRED.value
