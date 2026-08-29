"""Offline tests for the read-only MCP tool repeated-call guard."""

from __future__ import annotations

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

from erpnext_agent.mcp.adapter import MCPEnvelope
from erpnext_agent.mcp.loop_guard import REPEATED_TOOL_CALL, LoopGuardTool
from erpnext_agent.mcp.policy import DATA_AGENT_TOOLS, EXPECTED_TOOLS, READ_TOOLS
from erpnext_agent.mcp.tool_bridge import ERPNextMCPTool
from erpnext_agent.mcp.toolkit_factory import ERPNextToolkitFactory


class FakeReadTool(ToolBase):
    name = "erpnext_get_list"
    description = "fake read tool"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    is_concurrency_safe = True
    is_read_only = True
    is_mcp = True
    mcp_name = "erpnext"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="read allowed",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        self.calls.append(dict(kwargs))
        return ToolChunk(
            content=[TextBlock(text=json.dumps({"ok": True, "n": len(self.calls)}))],
            state=ToolResultState.SUCCESS,
        )


class FakeWriteTool(FakeReadTool):
    name = "erpnext_create_draft"
    is_read_only = False
    is_concurrency_safe = False


class FlakyReadTool(FakeReadTool):
    """Fails the first call so error handling can be observed by the guard."""

    def __init__(self) -> None:
        super().__init__()
        self._failures_remaining = 1

    async def call(self, **kwargs: Any) -> ToolChunk:
        self.calls.append(dict(kwargs))
        if self._failures_remaining:
            self._failures_remaining -= 1
            return ToolChunk(
                content=[
                    TextBlock(
                        text=json.dumps(
                            {"ok": False, "error": {"code": "INVALID_TOOL_ARGUMENT"}}
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )
        return ToolChunk(
            content=[TextBlock(text=json.dumps({"ok": True, "n": len(self.calls)}))],
            state=ToolResultState.SUCCESS,
        )


def _payload(chunk: ToolChunk) -> dict[str, Any]:
    assert isinstance(chunk.content[0], TextBlock)
    return json.loads(chunk.content[0].text)


@pytest.mark.asyncio
async def test_loop_guard_allows_first_two_calls() -> None:
    inner = FakeReadTool()
    guard = LoopGuardTool(tool=inner, max_repeats=3)
    first = await guard.call(query="Sales Order")
    second = await guard.call(query="Sales Order")
    assert first.state == ToolResultState.SUCCESS
    assert second.state == ToolResultState.SUCCESS
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_loop_guard_blocks_third_identical_call() -> None:
    inner = FakeReadTool()
    guard = LoopGuardTool(tool=inner, max_repeats=3)
    await guard.call(query="Sales Order")
    await guard.call(query="Sales Order")
    blocked = await guard.call(query="Sales Order")
    assert blocked.state == ToolResultState.ERROR
    # The inner tool must not have been invoked a third time.
    assert len(inner.calls) == 2
    # Further identical calls stay blocked.
    again = await guard.call(query="Sales Order")
    assert again.state == ToolResultState.ERROR
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_loop_guard_different_args_not_counted() -> None:
    inner = FakeReadTool()
    guard = LoopGuardTool(tool=inner, max_repeats=3)
    for value in ("a", "b", "c"):
        chunk = await guard.call(query=value)
        assert chunk.state == ToolResultState.SUCCESS
    assert len(inner.calls) == 3


@pytest.mark.asyncio
async def test_loop_guard_reset_clears_history() -> None:
    inner = FakeReadTool()
    guard = LoopGuardTool(tool=inner, max_repeats=2)
    await guard.call(query="x")
    blocked = await guard.call(query="x")
    assert blocked.state == ToolResultState.ERROR
    guard.reset()
    revived = await guard.call(query="x")
    assert revived.state == ToolResultState.SUCCESS
    assert len(inner.calls) == 2


def test_loop_guard_rejects_write_tool() -> None:
    with pytest.raises(ValueError):
        LoopGuardTool(tool=FakeWriteTool(), max_repeats=3)


@pytest.mark.asyncio
async def test_loop_guard_canonicalizes_key_order() -> None:
    inner = FakeReadTool()
    guard = LoopGuardTool(tool=inner, max_repeats=2)
    await guard.call(a="1", b="2")
    # Same arguments in a different insertion order count as identical.
    blocked = await guard.call(b="2", a="1")
    assert blocked.state == ToolResultState.ERROR
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_loop_guard_error_chunk_shape() -> None:
    guard = LoopGuardTool(tool=FakeReadTool(), max_repeats=2)
    await guard.call(query="x")
    blocked = await guard.call(query="x")
    payload = _payload(blocked)
    assert payload["ok"] is False
    assert payload["error"]["code"] == REPEATED_TOOL_CALL
    assert payload["error"]["message"]
    assert blocked.metadata == {"code": REPEATED_TOOL_CALL}


@pytest.mark.asyncio
async def test_loop_guard_error_result_does_not_consume_allowance() -> None:
    inner = FlakyReadTool()
    guard = LoopGuardTool(tool=inner, max_repeats=2)
    failed = await guard.call(query="x")
    assert failed.state == ToolResultState.ERROR
    # The identical retry must reach the inner tool: the failed attempt was
    # rolled back out of the signature history.
    retried = await guard.call(query="x")
    assert retried.state == ToolResultState.SUCCESS
    assert len(inner.calls) == 2
    # Now a successful result exists and occupies the max_repeats=2 allowance.
    blocked = await guard.call(query="x")
    assert blocked.state == ToolResultState.ERROR
    assert blocked.metadata == {"code": REPEATED_TOOL_CALL}
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_loop_guard_repeated_message_stays_corrective() -> None:
    guard = LoopGuardTool(tool=FakeReadTool(), max_repeats=2)
    await guard.call(query="x")
    blocked = await guard.call(query="x")
    message = _payload(blocked)["error"]["message"]
    # Never claim a valid result was already returned, and never push an
    # empty-rows answer onto the model.
    assert "已经返回过有效结果" not in message
    assert "就明确回答没有记录" not in message
    assert "相同参数" in message
    assert "更换过滤条件" in message


@pytest.mark.asyncio
async def test_loop_guard_delegates_permissions_and_surface() -> None:
    inner = FakeReadTool()
    guard = LoopGuardTool(tool=inner, max_repeats=3)
    decision = await guard.check_permissions({}, PermissionContext())
    assert decision.behavior == PermissionBehavior.ALLOW
    assert guard.name == inner.name
    assert guard.is_read_only is True
    assert guard.is_mcp is True
    assert guard.input_schema == inner.input_schema


class _StubAdapter:
    async def call_tool(self, **kwargs: Any) -> MCPEnvelope:
        raise AssertionError("not used in wiring tests")


def _tool_spec(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Description for {name}",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": name in READ_TOOLS},
    }


@pytest.mark.asyncio
async def test_toolkit_factory_wraps_only_read_tools() -> None:
    specs = [_tool_spec(name) for name in sorted(EXPECTED_TOOLS)]
    toolkits = ERPNextToolkitFactory(_StubAdapter()).build_from_specs(  # type: ignore[arg-type]
        specs=specs,
        access_token="token",  # noqa: S106 - inert fixture
        max_repeats=4,
    )
    data_names = sorted(DATA_AGENT_TOOLS)
    for name in data_names:
        data_tool = await toolkits.data.get_tool(name)
        assert isinstance(data_tool, LoopGuardTool)
        assert data_tool.name == name
        patrol_tool = await toolkits.patrol.get_tool(name)
        assert isinstance(patrol_tool, LoopGuardTool)

    # Without a proposal tool the action toolkit keeps raw write tools unwrapped.
    for name in ("erpnext_create_draft", "erpnext_update_draft"):
        action_write = await toolkits.action.get_tool(name)
        assert isinstance(action_write, ERPNextMCPTool)
        assert not isinstance(action_write, LoopGuardTool)
    for name in sorted(READ_TOOLS):
        action_read = await toolkits.action.get_tool(name)
        assert isinstance(action_read, LoopGuardTool)
