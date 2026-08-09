from __future__ import annotations

from typing import Any

from erpnext_agent.config import Settings


def build_mcp_client(
    *,
    settings: Settings,
    session_id: str,
    access_token: str,
    tools: list[str],
) -> Any:
    """Build one stateless AgentScope MCP client for exactly one user session."""

    from agentscope.mcp import HttpMCPConfig, MCPClient

    return MCPClient(
        name=f"erpnext-{session_id}",
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url=settings.effective_mcp_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=settings.mcp_http_timeout_seconds,
        ),
        enable_tools=tools,
        execution_timeout=settings.mcp_execution_timeout_seconds,
    )
