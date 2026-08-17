"""Repeated-tool-call guard for read-only ERPNext MCP tools.

Some models loop the identical read tool call until the ReAct budget is exhausted and
then finish with no user-facing text. AgentScope 2.0.5 has no built-in repeated-call
detection. :class:`LoopGuardTool` decorates a single read-only tool and, after the same
(name, arguments) pair has already been forwarded ``max_repeats - 1`` times, stops
forwarding and returns a firm error chunk that instructs the model to answer from the
data it already has or to change its arguments.

The wrapper is applied per request at toolkit assembly time, so its history never spans
requests, and it only ever decorates read-only tools (write tools are refused).
"""

from __future__ import annotations

import json
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionContext, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

REPEATED_TOOL_CALL = "REPEATED_TOOL_CALL"


class LoopGuardTool(ToolBase):
    """Decorate a read-only tool with a repeated-identical-call guard."""

    def __init__(self, *, tool: ToolBase, max_repeats: int = 3) -> None:
        super().__init__()
        if not tool.is_read_only:
            raise ValueError("LoopGuardTool may only wrap read-only tools")
        if max_repeats < 2:
            raise ValueError("max_repeats must be at least 2")
        self._tool = tool
        self._max_repeats = max_repeats
        self._seen: list[str] = []

        # Mirror the wrapped tool's agent-facing surface so registration, schema
        # advertisement and permission checks behave exactly like the original tool.
        self.name = tool.name
        self.description = tool.description
        self.input_schema = tool.input_schema
        self.is_read_only = tool.is_read_only
        self.is_concurrency_safe = tool.is_concurrency_safe
        self.is_external_tool = tool.is_external_tool
        self.is_state_injected = tool.is_state_injected
        self.is_mcp = tool.is_mcp
        self.mcp_name = getattr(tool, "mcp_name", None)

    def reset(self) -> None:
        self._seen.clear()

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        return await self._tool.check_permissions(tool_input, context)

    async def call(self, **kwargs: Any) -> ToolChunk:
        signature = self._canonicalize(kwargs)
        if self._seen.count(signature) >= self._max_repeats - 1:
            return self._repeated_chunk()
        self._seen.append(signature)
        result = await self._tool.call(**kwargs)
        # Read-only ERPNext MCP tools return a single ToolChunk; pass through any
        # other shape unchanged to stay compatible with the ToolBase contract.
        return result  # type: ignore[return-value]

    @staticmethod
    def _canonicalize(kwargs: dict[str, Any]) -> str:
        return json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)

    def _repeated_chunk(self) -> ToolChunk:
        error = {
            "ok": False,
            "error": {
                "code": REPEATED_TOOL_CALL,
                "message": (
                    "This exact tool call has already been made and returned data. "
                    "Do not repeat it. Answer from the information you already have, "
                    "or change the arguments if a different query is truly needed."
                ),
            },
        }
        return ToolChunk(
            content=[TextBlock(text=json.dumps(error, ensure_ascii=False))],
            state=ToolResultState.ERROR,
            metadata={"code": REPEATED_TOOL_CALL},
        )
