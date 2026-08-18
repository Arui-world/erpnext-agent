from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Protocol

import jsonschema  # type: ignore[import-untyped]
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from erpnext_agent.mcp.adapter import MCPEnvelope, MCPError
from erpnext_agent.mcp.policy import EXPECTED_TOOLS, READ_TOOLS

SAFE_META_FIELDS = frozenset({"trace_id", "tool", "user", "duration_ms", "replayed"})
JSON_COMPATIBLE_FIELDS = frozenset({"fields", "filters"})


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
        # Keep the canonical server contract for post-normalization validation. The
        # model-facing copy additionally accepts JSON-encoded nested arguments because
        # some OpenAI-compatible providers stringify arrays/objects in tool calls.
        self._server_input_schema = deepcopy(input_schema)
        self.input_schema = _model_compatible_schema(input_schema)
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
        normalized, error = _normalize_json_arguments(
            kwargs,
            self._server_input_schema,
        )
        if error is not None:
            return ToolChunk(
                content=[TextBlock(text=_json_text(error))],
                state=ToolResultState.ERROR,
                metadata={"code": "INVALID_TOOL_ARGUMENT"},
            )
        try:
            envelope = await self._adapter.call_tool(
                access_token=self._access_token,
                name=self.name,
                arguments=normalized,
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
            # This ToolBase waits for the MCP request to finish before returning.
            # RUNNING would make AgentScope keep the ReAct loop open until max_iters.
            state=ToolResultState.SUCCESS,
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


def _model_compatible_schema(server_schema: dict[str, Any]) -> dict[str, Any]:
    schema = deepcopy(server_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    for field in JSON_COMPATIBLE_FIELDS:
        field_schema = properties.get(field)
        if not isinstance(field_schema, dict):
            continue
        properties[field] = {
            "anyOf": [
                field_schema,
                {
                    "type": "string",
                    "description": (
                        "Compatibility form: a JSON-encoded array/object; the bridge "
                        "parses and validates it before the MCP request."
                    ),
                },
            ],
        }
    return schema


def _normalize_json_arguments(
    arguments: dict[str, Any],
    server_schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    normalized = dict(arguments)
    for field in JSON_COMPATIBLE_FIELDS:
        value = normalized.get(field)
        if not isinstance(value, str):
            continue
        try:
            normalized[field] = json.loads(value)
        except json.JSONDecodeError:
            return normalized, _invalid_argument_error(
                field,
                "must be valid JSON when supplied as a string",
            )
    try:
        jsonschema.validate(normalized, server_schema)
    except jsonschema.ValidationError as exc:
        field = str(exc.path[0]) if exc.path else "arguments"
        return normalized, _invalid_argument_error(field, exc.message)
    return normalized, None


def _invalid_argument_error(field: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "INVALID_TOOL_ARGUMENT",
            "message": f"Invalid {field}: {message}",
        },
    }
