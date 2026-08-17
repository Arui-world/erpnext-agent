from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
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
    _coerce_number,
    _normalize_model_arguments,
    build_action_preview,
    validate_action_arguments,
)
from erpnext_agent.actions.repository import ActionRepository, ActionStateError
from erpnext_agent.config import Settings
from erpnext_agent.mcp.adapter import (
    MCPBusinessError,
    MCPContractError,
    MCPEnvelope,
)
from erpnext_agent.observability import create_telemetry

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


def test_normalize_double_nested_arguments_unwrap() -> None:
    inner = material_request_arguments()
    normalized = _normalize_model_arguments({"arguments": inner})
    assert normalized["doctype"] == "Material Request"
    assert normalized["payload"]["items"][0]["item_code"] == "ITEM-0001"
    validate_action_arguments(CREATE_DRAFT_TOOL, normalized)


def test_normalize_single_object_items_wrapped_in_list() -> None:
    arguments = material_request_arguments()
    arguments["payload"]["items"] = dict(arguments["payload"]["items"][0])
    normalized = _normalize_model_arguments(arguments)
    assert isinstance(normalized["payload"]["items"], list)
    assert normalized["payload"]["items"][0]["item_code"] == "ITEM-0001"
    validate_action_arguments(CREATE_DRAFT_TOOL, normalized)


def test_normalize_qty_numeric_string_coercion() -> None:
    arguments = material_request_arguments()
    arguments["payload"]["items"][0]["qty"] = "2"
    normalized = _normalize_model_arguments(arguments)
    assert normalized["payload"]["items"][0]["qty"] == 2
    result = validate_action_arguments(CREATE_DRAFT_TOOL, normalized)
    assert result["payload"]["items"][0]["qty"] == 2


def test_normalize_quantity_alias_and_delivery_warehouse() -> None:
    arguments = material_request_arguments()
    item = arguments["payload"]["items"][0]
    item["quantity"] = item.pop("qty")
    item["delivery_warehouse"] = item.pop("warehouse")
    normalized = _normalize_model_arguments(arguments)
    row = normalized["payload"]["items"][0]
    assert row["qty"] == 2
    assert row["warehouse"] == "Stores - EX"
    assert "quantity" not in row
    assert "delivery_warehouse" not in row
    validate_action_arguments(CREATE_DRAFT_TOOL, normalized)


def test_normalize_model_arguments_is_noop_for_canonical_shape() -> None:
    arguments = material_request_arguments()
    assert _normalize_model_arguments(arguments) == arguments


def test_normalize_rejects_non_numeric_qty_string() -> None:
    arguments = material_request_arguments()
    arguments["payload"]["items"][0]["qty"] = "several"
    normalized = _normalize_model_arguments(arguments)
    with pytest.raises(ActionProposalError, match="positive number"):
        validate_action_arguments(CREATE_DRAFT_TOOL, normalized)


def test_coerce_number_parses_finite_numbers_only() -> None:
    assert _coerce_number("5") == 5
    assert _coerce_number(" 3 ") == 3
    assert _coerce_number("2.5") == 2.5
    assert _coerce_number("abc") is None
    assert _coerce_number("") is None
    assert _coerce_number("nan") is None
    assert _coerce_number("inf") is None


def test_proposal_tool_input_schema_is_agentscope_compatible() -> None:
    from agentscope.tool import RegisteredTool

    tool = ActionProposalTool(
        service=ActionProposalService(
            ActionGateway(cast(Any, FakeRepository()), ttl_seconds=900),
            ProposalCaller(),
        ),
        session=cast(AsyncSession, FakeSession()),
        session_id="session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        access_token=TEST_ACCESS_TOKEN,
        conversation_id="conversation-1",
    )
    # RegisteredTool validates input_schema in __post_init__ and rejects shapes that
    # are not a top-level object with a properties mapping.
    registered = RegisteredTool(tool=tool)
    schema = registered.get_tool_schema()
    function = schema["function"]
    assert function["name"] == PROPOSE_DRAFT_TOOL
    parameters = function["parameters"]
    assert set(parameters["properties"]) == {"tool_name", "arguments"}
    arguments_schema = parameters["properties"]["arguments"]
    assert "doctype" in arguments_schema["description"]
    assert "payload" in arguments_schema["description"]
    assert "idempotency_key" in arguments_schema["description"]


def _in_memory_telemetry() -> tuple[Any, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://agent:password@postgres/agent",
        redis_url=SecretStr("redis://:password@redis/0"),  # noqa: S106
        erpnext_base_url="http://dev.localhost:8000",
        erpnext_site="dev.localhost",
        oauth_client_id="client-id",
        oauth_client_secret=SecretStr("client-secret"),  # noqa: S106
        oauth_redirect_uri="http://localhost:8001/api/v1/auth/callback",
        session_secret=SecretStr("a-session-secret-with-at-least-32-characters"),  # noqa: S106
        token_encryption_key=SecretStr(  # noqa: S106
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        ),
        otel_enabled=True,
        otel_exporter_otlp_endpoint="http://collector:4318/v1/traces",
    )  # type: ignore[arg-type]
    telemetry = create_telemetry(
        settings,
        span_exporter=exporter,
        use_batch_processor=False,
    )
    return telemetry, exporter


def _proposal_tool_with_telemetry(telemetry: Any) -> ActionProposalTool:
    return ActionProposalTool(
        service=ActionProposalService(
            ActionGateway(cast(Any, FakeRepository()), ttl_seconds=900),
            ProposalCaller(),
        ),
        session=cast(AsyncSession, FakeSession()),
        session_id="session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        access_token=TEST_ACCESS_TOKEN,
        conversation_id="conversation-1",
        telemetry=telemetry,
    )


@pytest.mark.asyncio
async def test_proposal_tool_telemetry_span_on_success() -> None:
    telemetry, exporter = _in_memory_telemetry()
    tool = _proposal_tool_with_telemetry(telemetry)
    chunk = await tool.call(
        tool_name=CREATE_DRAFT_TOOL,
        arguments=material_request_arguments(),
    )
    assert chunk.state == ToolResultState.SUCCESS
    spans = [span for span in exporter.get_finished_spans() if span.name == "action.propose"]
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)
    assert attributes["doctype"] == "Material Request"
    assert attributes["tool_name"] == CREATE_DRAFT_TOOL
    assert attributes["result_code"] == "OK"
    assert "error_code" not in attributes


@pytest.mark.asyncio
async def test_proposal_tool_telemetry_span_on_failure() -> None:
    telemetry, exporter = _in_memory_telemetry()
    tool = _proposal_tool_with_telemetry(telemetry)
    arguments = material_request_arguments()
    arguments["payload"].pop("company")
    chunk = await tool.call(tool_name=CREATE_DRAFT_TOOL, arguments=arguments)
    assert chunk.state == ToolResultState.ERROR
    spans = [span for span in exporter.get_finished_spans() if span.name == "action.propose"]
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)
    assert attributes["error_code"] == "MISSING_REQUIRED_FIELDS"
    assert attributes["stage"] == "local_validation"
    assert attributes["doctype"] == "Material Request"
    assert attributes["result_code"] == "MISSING_REQUIRED_FIELDS"
    # Privacy guard: no payload or user data in span attributes.
    assert "company" not in str(attributes)
    assert "user@example.com" not in str(attributes)


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
