from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

import httpx

from erpnext_agent.mcp.policy import EXPECTED_TOOLS


class MCPError(RuntimeError):
    def __init__(self, message: str, *, code: str, trace_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.trace_id = trace_id


class MCPTransportError(MCPError):
    pass


class MCPProtocolError(MCPError):
    pass


class MCPToolError(MCPError):
    pass


class MCPBusinessError(MCPError):
    pass


class MCPContractError(MCPError):
    pass


@dataclass(frozen=True, slots=True)
class MCPEnvelope:
    data: Any
    meta: dict[str, Any]
    content_trust: str = "untrusted_business_data"


def normalize_tool_response(payload: dict[str, Any]) -> MCPEnvelope:
    """Apply the required JSON-RPC -> MCP -> business-envelope checks."""

    json_rpc_error = payload.get("error")
    if json_rpc_error:
        code = str(json_rpc_error.get("code", "JSON_RPC_ERROR"))
        raise MCPProtocolError("ERPNext MCP returned a protocol error", code=code)

    result = payload.get("result")
    if not isinstance(result, dict):
        raise MCPProtocolError("ERPNext MCP response has no result object", code="INVALID_RESULT")
    if result.get("isError") is True:
        raise MCPToolError("ERPNext MCP tool execution failed", code="MCP_TOOL_ERROR")

    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise MCPContractError(
            "ERPNext MCP response has no structuredContent",
            code="MISSING_STRUCTURED_CONTENT",
        )

    raw_meta = structured.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    trace_id = meta.get("trace_id") if isinstance(meta.get("trace_id"), str) else None
    if structured.get("ok") is not True:
        raw_error = structured.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        code = str(error.get("code") or "BUSINESS_ERROR")
        message = str(error.get("message") or "ERPNext rejected the operation")
        raise MCPBusinessError(message, code=code, trace_id=trace_id)

    return MCPEnvelope(data=structured.get("data"), meta=meta)


class ERPNextMCPAdapter:
    """A stateless JSON-RPC adapter that preserves structuredContent.

    AgentScope's built-in MCP Tool conversion does not retain structuredContent in
    version 2.0.5, so business calls must pass through this adapter before their
    sanitized data is exposed to an agent.
    """

    def __init__(
        self,
        *,
        url: str,
        http: httpx.AsyncClient,
        verify_contract: bool = True,
    ) -> None:
        self._url = url
        self._http = http
        self._verify_contract = verify_contract
        self._ids = itertools.count(1)

    async def initialize(self, access_token: str) -> dict[str, Any]:
        response = await self._rpc(
            access_token,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "erpnext-agent", "version": "0.1.0"},
            },
        )
        await self._notification(access_token, "notifications/initialized", {})
        return response

    async def discover_tools(self, access_token: str) -> list[dict[str, Any]]:
        await self.initialize(access_token)
        payload = await self._rpc(access_token, "tools/list", {})
        result = payload.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise MCPContractError("tools/list returned an invalid payload", code="INVALID_TOOLS")
        names = {
            name
            for tool in tools
            if isinstance(tool, dict)
            if isinstance((name := tool.get("name")), str)
        }
        if self._verify_contract and names != EXPECTED_TOOLS:
            missing = sorted(EXPECTED_TOOLS - names)
            unexpected = sorted(names - EXPECTED_TOOLS)
            raise MCPContractError(
                f"MCP tool contract mismatch; missing={missing}, unexpected={unexpected}",
                code="TOOL_CONTRACT_MISMATCH",
            )
        return tools

    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope:
        if discover_first:
            tools = await self.discover_tools(access_token)
            if name not in {item.get("name") for item in tools}:
                raise MCPContractError(f"MCP tool is unavailable: {name}", code="TOOL_UNAVAILABLE")
        payload = await self._rpc(
            access_token,
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        return normalize_tool_response(payload)

    async def current_user(self, access_token: str) -> str:
        envelope = await self.call_tool(
            access_token=access_token,
            name="erpnext_get_current_user",
            arguments={},
            discover_first=True,
        )
        if not isinstance(envelope.data, dict):
            raise MCPContractError("Current-user response has no user", code="INVALID_IDENTITY")
        user = envelope.data.get("user")
        if not isinstance(user, str):
            raise MCPContractError("Current-user response has no user", code="INVALID_IDENTITY")
        return user

    async def _rpc(
        self,
        access_token: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        try:
            response = await self._http.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise MCPTransportError("Unable to reach ERPNext MCP", code="MCP_UNAVAILABLE") from exc
        if response.status_code in {401, 403}:
            raise MCPTransportError("ERPNext authentication failed", code="MCP_AUTH_FAILED")
        if response.is_error:
            raise MCPTransportError(
                "ERPNext MCP returned an HTTP error",
                code=f"HTTP_{response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MCPProtocolError(
                "ERPNext MCP returned invalid JSON",
                code="INVALID_JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise MCPProtocolError("ERPNext MCP returned invalid JSON-RPC", code="INVALID_JSON_RPC")
        return payload

    async def _notification(
        self,
        access_token: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        try:
            response = await self._http.post(
                self._url,
                json={"jsonrpc": "2.0", "method": method, "params": params},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise MCPTransportError(
                "Unable to initialize ERPNext MCP",
                code="MCP_UNAVAILABLE",
            ) from exc
        if response.status_code not in {200, 202, 204}:
            raise MCPTransportError(
                "ERPNext MCP rejected initialization",
                code=f"HTTP_{response.status_code}",
            )
