import json
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk

from erpnext_agent.mcp.adapter import MCPBusinessError, MCPEnvelope
from erpnext_agent.mcp.policy import EXPECTED_TOOLS, READ_TOOLS, WRITE_TOOLS
from erpnext_agent.mcp.tool_bridge import ERPNextMCPTool
from erpnext_agent.mcp.toolkit_factory import ERPNextToolkitFactory

TEST_ACCESS_TOKEN = "not-a-real-token"  # noqa: S105 - inert test fixture


class FakeAdapter:
    def __init__(self, envelope: MCPEnvelope | None = None) -> None:
        self.envelope = envelope or MCPEnvelope(
            data={"name": "SO-1"},
            meta={"trace_id": "trace-1", "secret": "must-not-pass"},
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def discover_tools(self, access_token: str) -> list[dict[str, Any]]:
        del access_token
        return all_specs()

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
        return self.envelope


class FailingAdapter(FakeAdapter):
    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope:
        del access_token, name, arguments, discover_first
        raise MCPBusinessError(
            "当前用户无权限",
            code="PERMISSION_DENIED",
            trace_id="trace-denied",
        )


class ProposalStub(ToolBase):
    name = "erpnext_propose_draft_action"
    description = "Create a persistent Action preview without writing ERPNext."
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    is_concurrency_safe = False
    is_read_only = False

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        del tool_input, context
        return PermissionDecision(behavior=PermissionBehavior.ALLOW)

    async def call(self, **kwargs: Any) -> ToolChunk:
        del kwargs
        return ToolChunk(content=[TextBlock(text="ok")], state=ToolResultState.SUCCESS)


def tool_spec(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Description for {name}",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "$defs": {"marker": {"type": "string"}},
        },
        "annotations": {"readOnlyHint": name in READ_TOOLS},
    }


def all_specs() -> list[dict[str, Any]]:
    return [tool_spec(name) for name in sorted(EXPECTED_TOOLS)]


@pytest.mark.asyncio
async def test_tool_preserves_schema_and_structured_content() -> None:
    spec = tool_spec("erpnext_get_list")
    adapter = FakeAdapter()
    tool = ERPNextMCPTool(spec=spec, adapter=adapter, access_token=TEST_ACCESS_TOKEN)

    chunk = await tool.call(query="Sales Order")
    assert tool.input_schema == spec["inputSchema"]
    assert chunk.state == ToolResultState.SUCCESS
    assert isinstance(chunk.content[0], TextBlock)
    payload = json.loads(chunk.content[0].text)
    assert payload == {
        "ok": True,
        "content_trust": "untrusted_business_data",
        "data": {"name": "SO-1"},
        "meta": {"trace_id": "trace-1"},
    }
    assert "secret" not in chunk.metadata


@pytest.mark.asyncio
async def test_ok_false_business_error_becomes_error_tool_chunk() -> None:
    tool = ERPNextMCPTool(
        spec=tool_spec("erpnext_get_doc"),
        adapter=FailingAdapter(),
        access_token=TEST_ACCESS_TOKEN,
    )
    chunk = await tool.call(query="SO-1")
    assert chunk.state == ToolResultState.ERROR
    assert isinstance(chunk.content[0], TextBlock)
    payload = json.loads(chunk.content[0].text)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PERMISSION_DENIED"
    assert payload["meta"]["trace_id"] == "trace-denied"


@pytest.mark.asyncio
async def test_read_and_write_permissions_fail_closed() -> None:
    context = PermissionContext()
    read_tool = ERPNextMCPTool(
        spec=tool_spec("erpnext_get_list"),
        adapter=FakeAdapter(),
        access_token=TEST_ACCESS_TOKEN,
    )
    write_tool = ERPNextMCPTool(
        spec=tool_spec("erpnext_create_draft"),
        adapter=FakeAdapter(),
        access_token=TEST_ACCESS_TOKEN,
    )
    read_decision = await read_tool.check_permissions({}, context)
    write_decision = await write_tool.check_permissions({}, context)
    assert read_decision.behavior == PermissionBehavior.ALLOW
    assert write_decision.behavior == PermissionBehavior.ASK
    assert write_decision.bypass_immune is True


@pytest.mark.asyncio
async def test_toolkit_factory_physically_separates_write_tools() -> None:
    adapter = FakeAdapter()
    toolkits = ERPNextToolkitFactory(adapter).build_from_specs(  # type: ignore[arg-type]
        specs=all_specs(),
        access_token=TEST_ACCESS_TOKEN,
    )
    for name in WRITE_TOOLS:
        assert await toolkits.data.get_tool(name) is None
        assert await toolkits.patrol.get_tool(name) is None
        assert await toolkits.orchestrator.get_tool(name) is None
        assert await toolkits.action.get_tool(name) is not None
    for name in READ_TOOLS:
        if name not in {"erpnext_health", "erpnext_get_current_user"}:
            assert await toolkits.data.get_tool(name) is not None
    for name in EXPECTED_TOOLS:
        assert await toolkits.orchestrator.get_tool(name) is None


@pytest.mark.asyncio
async def test_action_planning_toolkit_replaces_direct_write_tools() -> None:
    proposal = ProposalStub()
    toolkits = ERPNextToolkitFactory(FakeAdapter()).build_from_specs(  # type: ignore[arg-type]
        specs=all_specs(),
        access_token=TEST_ACCESS_TOKEN,
        action_proposal_tool=proposal,
    )

    assert await toolkits.action.get_tool(proposal.name) is not None
    for name in WRITE_TOOLS:
        assert await toolkits.action.get_tool(name) is None
    assert await toolkits.data.get_tool(proposal.name) is None
    assert await toolkits.patrol.get_tool(proposal.name) is None
    assert await toolkits.orchestrator.get_tool(proposal.name) is None
