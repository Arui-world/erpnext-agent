from typing import Any

import pytest

from erpnext_agent.mcp.adapter import (
    MCPBusinessError,
    MCPContractError,
    MCPProtocolError,
    MCPToolError,
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
