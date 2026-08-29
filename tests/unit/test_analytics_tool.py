"""Offline tests for the deterministic local analytics tool."""

from __future__ import annotations

import json
from typing import Any

import pytest
from agentscope.message import ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.tool import ToolChunk

from erpnext_agent.analytics.tool import AnalyticsCalculationTool, compute_analytics
from erpnext_agent.mcp.policy import ANALYTICS_TOOL_NAME


def _payload(chunk: ToolChunk) -> dict[str, Any]:
    assert isinstance(chunk.content[0].text, str)
    return json.loads(chunk.content[0].text)


def test_period_change_is_decimal_exact() -> None:
    data = compute_analytics({"kind": "period_change", "current": 12, "previous": 10})
    assert data["formula"] == "(current - previous) / previous"
    assert data["decimal"] == "0.20"
    assert data["percent"] == "20.00"
    assert data["direction"] == "increase"


def test_period_change_decrease_and_flat_and_negative() -> None:
    down = compute_analytics({"kind": "period_change", "current": 3, "previous": 5})
    assert down["decimal"] == "-0.40"
    assert down["percent"] == "-40.00"
    assert down["direction"] == "decrease"
    flat = compute_analytics({"kind": "period_change", "current": 7, "previous": 7})
    assert flat["direction"] == "flat"
    assert flat["percent"] == "0.00"


def test_ratio_and_string_inputs_and_digits() -> None:
    data = compute_analytics({"kind": "ratio", "part": "1", "total": "3", "digits": 4})
    assert data["decimal"] == "0.3333"
    assert data["percent"] == "33.3333"
    assert data["formula"] == "part / total"
    zero_digits = compute_analytics({"kind": "ratio", "part": 1, "total": 3, "digits": 0})
    assert zero_digits["percent"] == "33"


def test_zero_base_is_an_honest_null_not_an_error() -> None:
    data = compute_analytics({"kind": "period_change", "current": 5, "previous": 0})
    assert data["decimal"] is None
    assert data["percent"] is None
    assert data["note"] == "zero_base"


def test_label_is_echoed() -> None:
    data = compute_analytics({"kind": "ratio", "part": 1, "total": 2, "label": "完成率"})
    assert data["label"] == "完成率"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "period_change", "current": 1},
        {"kind": "ratio", "part": 1},
        {},
        {"kind": "growth", "current": 1, "previous": 2},
        {"kind": "ratio", "part": "abc", "total": 2},
        {"kind": "ratio", "part": 1, "total": True},
        {"kind": "ratio", "part": 1, "total": 2, "digits": 9},
        {"kind": "ratio", "part": 1, "total": 2, "digits": -1},
        {"kind": "ratio", "part": 1, "total": 2, "label": "x" * 81},
    ],
)
def test_invalid_inputs_raise_input_errors(payload: dict[str, Any]) -> None:
    from erpnext_agent.analytics.tool import AnalyticsInputError

    with pytest.raises(AnalyticsInputError):
        compute_analytics(payload)


@pytest.mark.asyncio
async def test_tool_call_success_envelope() -> None:
    tool = AnalyticsCalculationTool()
    chunk = await tool.call(kind="period_change", current=8, previous=4)
    assert chunk.state == ToolResultState.SUCCESS
    payload = _payload(chunk)
    assert payload["ok"] is True
    assert payload["data"]["percent"] == "100.00"
    assert payload["data"]["direction"] == "increase"


@pytest.mark.asyncio
async def test_tool_call_missing_operands_error_chunk() -> None:
    tool = AnalyticsCalculationTool()
    chunk = await tool.call(kind="period_change", current=8)
    assert chunk.state == ToolResultState.ERROR
    payload = _payload(chunk)
    assert payload["error"]["code"] == "MISSING_OPERANDS"
    assert chunk.metadata == {"code": "MISSING_OPERANDS"}


@pytest.mark.asyncio
async def test_tool_is_read_only_and_auto_allowed() -> None:
    tool = AnalyticsCalculationTool()
    assert tool.name == ANALYTICS_TOOL_NAME
    assert tool.is_read_only is True
    decision = await tool.check_permissions({"kind": "ratio"}, PermissionContext())
    assert decision.behavior == PermissionBehavior.ALLOW
