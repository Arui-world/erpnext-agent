from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from agentscope.message import AssistantMsg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool import ToolChoice, ToolChunk, ToolResponse

from erpnext_agent.agents.convergence import TerminalToolConvergenceMiddleware


async def _successful_tool(**_: Any) -> AsyncGenerator[ToolResponse, None]:
    yield ToolResponse(
        content=[TextBlock(text='{"ok":true,"count":1}')],
        state=ToolResultState.SUCCESS,
    )


async def _failed_tool(**_: Any) -> AsyncGenerator[ToolResponse, None]:
    yield ToolResponse(
        content=[TextBlock(text='{"ok":false}')],
        state=ToolResultState.ERROR,
    )


async def _successful_chunk(**_: Any) -> AsyncGenerator[ToolChunk, None]:
    yield ToolChunk(
        content=[TextBlock(text='{"ok":true,"count":1}')],
        state=ToolResultState.SUCCESS,
        is_last=True,
    )


def _agent_stub() -> Any:
    class State:
        reply_id = "reply-1"
        context = [AssistantMsg(name="data_agent", content="tool result")]

    class Stub:
        name = "data_agent"
        state = State()

    return Stub()


@pytest.mark.asyncio
async def test_successful_terminal_tool_forces_next_reasoning_to_text_only() -> None:
    middleware = TerminalToolConvergenceMiddleware(frozenset({"erpnext_get_count"}))
    agent = _agent_stub()
    tool_call = ToolCallBlock(
        id="call-1",
        name="erpnext_get_count",
        input="{}",
    )
    acting_items = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": tool_call},
            _successful_tool,
        )
    ]
    assert len(acting_items) == 1

    captured: dict[str, Any] = {}

    async def reasoning(**kwargs: Any) -> AsyncGenerator[str, None]:
        captured.update(kwargs)
        yield "final"

    events = [
        item
        async for item in middleware.on_reasoning(
            agent,
            {"tool_choice": None},
            reasoning,
        )
    ]
    assert events == ["final"]
    assert isinstance(captured["tool_choice"], ToolChoice)
    assert captured["tool_choice"].mode == "none"
    assert agent.state.context[-1].content[-1].type == "hint"


@pytest.mark.asyncio
async def test_successful_terminal_tool_chunk_also_forces_text_only() -> None:
    middleware = TerminalToolConvergenceMiddleware(frozenset({"erpnext_get_count"}))
    agent = _agent_stub()
    tool_call = ToolCallBlock(id="call-1", name="erpnext_get_count", input="{}")
    _ = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": tool_call},
            _successful_chunk,
        )
    ]
    captured: dict[str, Any] = {}

    async def reasoning(**kwargs: Any) -> AsyncGenerator[None, None]:
        captured.update(kwargs)
        if False:
            yield None

    _ = [
        item
        async for item in middleware.on_reasoning(
            agent,
            {"tool_choice": None},
            reasoning,
        )
    ]
    assert captured["tool_choice"].mode == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "handler"),
    [
        ("erpnext_get_stock_balance", _successful_tool),
        ("erpnext_get_count", _failed_tool),
    ],
)
async def test_nonterminal_or_failed_tool_keeps_automatic_tool_choice(
    tool_name: str,
    handler: Any,
) -> None:
    middleware = TerminalToolConvergenceMiddleware(frozenset({"erpnext_get_count"}))
    agent = _agent_stub()
    tool_call = ToolCallBlock(id="call-1", name=tool_name, input="{}")
    _ = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": tool_call},
            handler,
        )
    ]
    captured: dict[str, Any] = {}

    async def reasoning(**kwargs: Any) -> AsyncGenerator[None, None]:
        captured.update(kwargs)
        if False:
            yield None

    _ = [
        item
        async for item in middleware.on_reasoning(
            agent,
            {"tool_choice": None},
            reasoning,
        )
    ]
    assert captured["tool_choice"] is None
