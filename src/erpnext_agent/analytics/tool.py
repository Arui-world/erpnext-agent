"""Deterministic percentage calculations exposed as a local read-only tool.

The business agents fetch bounded numbers through permission-aware MCP reads,
but percentage arithmetic (环比/占比) must not be done by model mental math.
This tool wraps :mod:`erpnext_agent.analytics.calculators` (Decimal only) so
every derived percentage is exact, echo-formatted, and auditable — the
whitelist-calculator path mandated by the development plan (D11), instead of
executing model-generated code.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from erpnext_agent.analytics.calculators import period_change, ratio
from erpnext_agent.mcp.policy import ANALYTICS_TOOL_NAME

_KIND_PERIOD_CHANGE = "period_change"
_KIND_RATIO = "ratio"


class AnalyticsInputError(ValueError):
    """A stable, model-readable input problem."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def _to_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AnalyticsInputError("INVALID_ANALYTICS_INPUT", f"{field} 必须是数字")
    if isinstance(value, float) and not math.isfinite(value):
        raise AnalyticsInputError("INVALID_ANALYTICS_INPUT", f"{field} 必须是有限数字")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise AnalyticsInputError("INVALID_ANALYTICS_INPUT", f"{field} 不是有效数字") from exc


def _quantize(value: Decimal, digits: int) -> str:
    exponent = Decimal(1).scaleb(-digits)
    return str(value.quantize(exponent))


def compute_analytics(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure calculation core; raises AnalyticsInputError on bad input."""
    kind = payload.get("kind")
    if kind not in (_KIND_PERIOD_CHANGE, _KIND_RATIO):
        raise AnalyticsInputError(
            "INVALID_ANALYTICS_INPUT",
            f"kind 只能是 {_KIND_PERIOD_CHANGE} 或 {_KIND_RATIO}",
        )
    digits = payload.get("digits", 2)
    if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6:
        raise AnalyticsInputError("INVALID_ANALYTICS_INPUT", "digits 必须是 0 到 6 的整数")
    label = payload.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > 80):
        raise AnalyticsInputError("INVALID_ANALYTICS_INPUT", "label 必须是不超过 80 字符的字符串")

    if kind == _KIND_PERIOD_CHANGE:
        if "current" not in payload or "previous" not in payload:
            raise AnalyticsInputError(
                "MISSING_OPERANDS", "period_change 需要 current 和 previous 两个数字"
            )
        current = _to_decimal(payload["current"], "current")
        previous = _to_decimal(payload["previous"], "previous")
        value = period_change(current, previous)
        formula = "(current - previous) / previous"
    else:
        if "part" not in payload or "total" not in payload:
            raise AnalyticsInputError("MISSING_OPERANDS", "ratio 需要 part 和 total 两个数字")
        part = _to_decimal(payload["part"], "part")
        total = _to_decimal(payload["total"], "total")
        value = ratio(part, total)
        formula = "part / total"

    result: dict[str, Any] = {"kind": kind, "formula": formula}
    if label is not None:
        result["label"] = label
    if value is None:
        # A zero base is an honest outcome, not an error: report it as such.
        result.update({"decimal": None, "percent": None, "note": "zero_base"})
        return result
    result["decimal"] = _quantize(value, digits)
    result["percent"] = _quantize(value * 100, digits)
    if kind == _KIND_PERIOD_CHANGE:
        if value > 0:
            result["direction"] = "increase"
        elif value < 0:
            result["direction"] = "decrease"
        else:
            result["direction"] = "flat"
    return result


def _error_chunk(code: str, message: str) -> ToolChunk:
    error = {"ok": False, "error": {"code": code, "message": message}}
    return ToolChunk(
        content=[TextBlock(text=json.dumps(error, ensure_ascii=False))],
        state=ToolResultState.ERROR,
        metadata={"code": code},
    )


class AnalyticsCalculationTool(ToolBase):
    """Local, read-only, deterministic percentage calculator."""

    name = ANALYTICS_TOOL_NAME
    description = (
        "Deterministic calculator for 环比/同比变化率 and 占比. Provide kind "
        "('period_change' with current and previous, or 'ratio' with part and "
        "total); the tool returns exact decimal, percent (×100, with sign for "
        "period_change) and the formula used. Never compute percentages in "
        "your head: fetch the numbers with read tools, then cite this result."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [_KIND_PERIOD_CHANGE, _KIND_RATIO]},
            "current": {"type": "number", "description": "period_change: 本期值"},
            "previous": {"type": "number", "description": "period_change: 基期值"},
            "part": {"type": "number", "description": "ratio: 分子"},
            "total": {"type": "number", "description": "ratio: 分母"},
            "label": {"type": "string", "maxLength": 80},
            "digits": {"type": "integer", "minimum": 0, "maximum": 6, "default": 2},
        },
        "required": ["kind"],
        "additionalProperties": False,
    }
    is_concurrency_safe = True
    is_read_only = True

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Deterministic calculation reads nothing and modifies nothing",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            data = compute_analytics(dict(kwargs))
        except AnalyticsInputError as exc:
            return _error_chunk(exc.code, exc.public_message)
        payload = {"ok": True, "data": data}
        return ToolChunk(
            content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )
