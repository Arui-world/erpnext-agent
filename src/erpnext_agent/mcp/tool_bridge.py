from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Protocol

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from erpnext_agent.mcp.adapter import MCPEnvelope, MCPError
from erpnext_agent.mcp.policy import EXPECTED_TOOLS, READ_TOOLS

SAFE_META_FIELDS = frozenset({"trace_id", "tool", "user", "duration_ms", "replayed"})


class MCPToolCaller(Protocol):
    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope: ...


class ERPNextMCPTool(ToolBase):
    """AgentScope ToolBase preserving ERPNext MCP structuredContent semantics."""

    is_mcp = True
    mcp_name = "erpnext"
    is_external_tool = False
    is_state_injected = False

    def __init__(
        self,
        *,
        spec: dict[str, Any],
        adapter: MCPToolCaller,
        access_token: str,
    ) -> None:
        super().__init__()
        name = spec.get("name")
        description = spec.get("description")
        input_schema = spec.get("inputSchema")
        if not isinstance(name, str) or name not in EXPECTED_TOOLS:
            raise ValueError("MCP tool is not present in the enforced local contract")
        if not isinstance(input_schema, dict):
            raise ValueError(f"MCP tool {name} has no valid inputSchema")

        self.name = name
        self.description = description if isinstance(description, str) else ""
        # Preserve the server schema exactly. In particular, do not drop $defs,
        # anyOf/oneOf or silently add defaults that change the contract hash.
        self.input_schema = deepcopy(input_schema)
        self.is_read_only = name in READ_TOOLS
        self.is_concurrency_safe = self.is_read_only
        self._adapter = adapter
        self._access_token = access_token

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        del tool_input, context
        if self.is_read_only:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="ERPNext read-only tool allowed by local policy.",
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="ERPNext draft writes require the persistent Action approval gateway.",
            bypass_immune=True,
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            envelope = await self._adapter.call_tool(
                access_token=self._access_token,
                name=self.name,
                arguments=kwargs,
            )
        except MCPError as exc:
            error = {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
                "meta": {"trace_id": exc.trace_id} if exc.trace_id else {},
            }
            return ToolChunk(
                content=[TextBlock(text=_json_text(error))],
                state=ToolResultState.ERROR,
                metadata={"trace_id": exc.trace_id} if exc.trace_id else {},
            )

        safe_meta = {
            key: value
            for key, value in envelope.meta.items()
            if key in SAFE_META_FIELDS
        }
        result = {
            "ok": True,
            "content_trust": envelope.content_trust,
            "data": envelope.data,
            "meta": safe_meta,
        }
        return ToolChunk(
            content=[TextBlock(text=_json_text(result))],
            state=ToolResultState.RUNNING,
            metadata={
                "content_trust": envelope.content_trust,
                **safe_meta,
            },
        )


def validate_tool_spec(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = spec.get("name")
    schema = spec.get("inputSchema")
    if not isinstance(name, str) or name not in EXPECTED_TOOLS:
        raise ValueError("Unexpected MCP tool name")
    if not isinstance(schema, dict):
        raise ValueError(f"MCP tool {name} has no valid inputSchema")
    return name, schema


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
