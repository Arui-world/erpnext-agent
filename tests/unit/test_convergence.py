from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from agentscope.message import AssistantMsg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool import ToolChoice, ToolChunk, ToolResponse

from erpnext_agent.agents.convergence import TerminalToolConvergenceMiddleware

_LIST_TERMINAL = frozenset({"erpnext_get_count", "erpnext_get_list"})
_EMPTY_RETRY = frozenset({"erpnext_get_list"})


def _list_chunk(rows: list[Any]) -> ToolChunk:
    payload = {
        "ok": True,
        "content_trust": "untrusted_business_data",
        "data": {"doctype": "Item", "rows": rows, "pagination": {"returned": len(rows)}},
    }
    return ToolChunk(
        content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
        state=ToolResultState.SUCCESS,
        is_last=True,
    )


def _handler(item: Any) -> Any:
    async def handler(**_: Any) -> AsyncGenerator[Any, None]:
        yield item

    return handler


async def _acting(
    middleware: TerminalToolConvergenceMiddleware,
    agent: Any,
    tool_name: str,
    handler: Any,
) -> None:
    tool_call = ToolCallBlock(id="call-1", name=tool_name, input="{}")
    _ = [
        item
        async for item in middleware.on_acting(agent, {"tool_call": tool_call}, handler)
    ]


async def _reasoning(
    middleware: TerminalToolConvergenceMiddleware,
    agent: Any,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def next_handler(**kwargs: Any) -> AsyncGenerator[None, None]:
        captured.update(kwargs)
        if False:
            yield None

    _ = [
        item
        async for item in middleware.on_reasoning(agent, {"tool_choice": None}, next_handler)
    ]
    return captured


def _hints(agent: Any) -> list[str]:
    hints: list[str] = []
    for message in agent.state.context:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if getattr(block, "type", None) == "hint":
                hints.append(block.hint)
    return hints


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


@pytest.mark.asyncio
async def test_non_empty_list_result_converges() -> None:
    middleware = TerminalToolConvergenceMiddleware(_LIST_TERMINAL, empty_retry_tools=_EMPTY_RETRY)
    agent = _agent_stub()
    await _acting(
        middleware, agent, "erpnext_get_list", _handler(_list_chunk([{"name": "ITEM-1"}]))
    )
    captured = await _reasoning(middleware, agent)
    assert captured["tool_choice"].mode == "none"


@pytest.mark.asyncio
async def test_first_empty_list_result_grants_one_corrective_retry() -> None:
    middleware = TerminalToolConvergenceMiddleware(_LIST_TERMINAL, empty_retry_tools=_EMPTY_RETRY)
    agent = _agent_stub()
    await _acting(middleware, agent, "erpnext_get_list", _handler(_list_chunk([])))
    captured = await _reasoning(middleware, agent)
    # The retry grant must not force a text-only pass.
    assert captured["tool_choice"] is None
    hints = _hints(agent)
    assert len(hints) == 1
    assert "like" in hints[0]
    # The grant is announced once; a following reasoning pass adds no second hint.
    await _reasoning(middleware, agent)
    assert len(_hints(agent)) == 1


@pytest.mark.asyncio
async def test_second_empty_list_result_converges_with_honest_answer() -> None:
    middleware = TerminalToolConvergenceMiddleware(_LIST_TERMINAL, empty_retry_tools=_EMPTY_RETRY)
    agent = _agent_stub()
    await _acting(middleware, agent, "erpnext_get_list", _handler(_list_chunk([])))
    await _reasoning(middleware, agent)
    await _acting(middleware, agent, "erpnext_get_list", _handler(_list_chunk([])))
    captured = await _reasoning(middleware, agent)
    assert captured["tool_choice"].mode == "none"
    hints = _hints(agent)
    assert any("如实说明没有查询到符合条件的记录" in hint for hint in hints)


@pytest.mark.asyncio
async def test_count_zero_converges_immediately() -> None:
    middleware = TerminalToolConvergenceMiddleware(_LIST_TERMINAL, empty_retry_tools=_EMPTY_RETRY)
    agent = _agent_stub()
    chunk = ToolChunk(
        content=[TextBlock(text='{"ok":true,"data":{"doctype":"Item","count":0}}')],
        state=ToolResultState.SUCCESS,
        is_last=True,
    )
    await _acting(middleware, agent, "erpnext_get_count", _handler(chunk))
    captured = await _reasoning(middleware, agent)
    assert captured["tool_choice"].mode == "none"


@pytest.mark.asyncio
async def test_aggregate_tool_empty_rows_still_converges() -> None:
    terminal = _LIST_TERMINAL | frozenset({"erpnext_get_item_group_low_stock"})
    middleware = TerminalToolConvergenceMiddleware(terminal, empty_retry_tools=_EMPTY_RETRY)
    agent = _agent_stub()
    await _acting(
        middleware,
        agent,
        "erpnext_get_item_group_low_stock",
        _handler(_list_chunk([])),
    )
    captured = await _reasoning(middleware, agent)
    assert captured["tool_choice"].mode == "none"


@pytest.mark.asyncio
async def test_unparseable_list_payload_falls_back_to_convergence() -> None:
    middleware = TerminalToolConvergenceMiddleware(_LIST_TERMINAL, empty_retry_tools=_EMPTY_RETRY)
    agent = _agent_stub()
    chunk = ToolChunk(
        content=[TextBlock(text="not-json-at-all")],
        state=ToolResultState.SUCCESS,
        is_last=True,
    )
    await _acting(middleware, agent, "erpnext_get_list", _handler(chunk))
    captured = await _reasoning(middleware, agent)
    assert captured["tool_choice"].mode == "none"
