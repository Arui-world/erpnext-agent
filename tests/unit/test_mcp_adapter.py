import json
from typing import Any

import httpx
import pytest

from erpnext_agent.mcp.adapter import (
    ERPNextMCPAdapter,
    MCPBusinessError,
    MCPContractError,
    MCPProtocolError,
    MCPToolError,
    MCPTransportError,
    normalize_tool_response,
)


def test_normalize_success_uses_only_structured_content() -> None:
    envelope = normalize_tool_response(
        {
            "jsonrpc": "2.0",
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": "duplicate text"}],
                "structuredContent": {
                    "ok": True,
                    "data": {"name": "SO-1"},
                    "meta": {"trace_id": "trace-1"},
                },
            },
        }
    )
    assert envelope.data == {"name": "SO-1"}
    assert envelope.meta == {"trace_id": "trace-1"}
    assert envelope.content_trust == "untrusted_business_data"


def test_business_failure_is_not_treated_as_success() -> None:
    with pytest.raises(MCPBusinessError) as raised:
        normalize_tool_response(
            {
                "result": {
                    "isError": False,
                    "structuredContent": {
                        "ok": False,
                        "error": {"code": "PERMISSION_DENIED", "message": "denied"},
                        "meta": {"trace_id": "trace-2"},
                    },
                }
            }
        )
    assert raised.value.code == "PERMISSION_DENIED"
    assert raised.value.trace_id == "trace-2"


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"error": {"code": -32600}}, MCPProtocolError),
        ({"result": {"isError": True}}, MCPToolError),
        ({"result": {"isError": False}}, MCPContractError),
    ],
)
def test_all_protocol_layers_fail_closed(
    payload: dict[str, Any],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        normalize_tool_response(payload)


@pytest.mark.asyncio
async def test_initialize_notification_maps_auth_failure_for_refresh_retry() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("method") == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = ERPNextMCPAdapter(
            url="http://mcp.local/mcp",
            http=http,
            verify_contract=False,
        )
        with pytest.raises(MCPTransportError) as raised:
            await adapter.initialize("expired")  # noqa: S106 - inert test credential
    assert raised.value.code == "MCP_AUTH_FAILED"
