from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.auth.token_refresh import TokenRefreshError, TokenRefreshService
from erpnext_agent.auth.token_store import StoredCredential
from erpnext_agent.mcp.adapter import (
    ERPNextMCPAdapter,
    MCPEnvelope,
    MCPError,
    MCPTransportError,
)
from erpnext_agent.observability import Telemetry, set_span_result


class RefreshingMCPCaller:
    """Retry one MCP tool call with a freshly rotated user access token."""

    def __init__(
        self,
        *,
        adapter: ERPNextMCPAdapter,
        refresh_service: TokenRefreshService,
        db: AsyncSession,
        credential: StoredCredential,
        session_store: SessionStore,
        agent_session_id: str,
    ) -> None:
        self._adapter = adapter
        self._refresh_service = refresh_service
        self._db = db
        self._credential = credential
        self._session_store = session_store
        self._agent_session_id = agent_session_id

    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope:
        del access_token
        telemetry = getattr(self._adapter, "telemetry", None) or Telemetry.disabled()
        with telemetry.span(
            "mcp.call",
            attributes={"tool_name": name, "retry_count": 0},
        ) as span:
            try:
                envelope = await self._adapter.call_tool(
                    access_token=self._credential.access_token,
                    name=name,
                    arguments=arguments,
                    discover_first=discover_first,
                )
            except MCPError as exc:
                if exc.code != "MCP_AUTH_FAILED":
                    set_span_result(span, exc.code, error=True)
                    raise
            else:
                set_span_result(span, "OK")
                return envelope

            span.set_attribute("retry_count", 1)
            try:
                self._credential = await self._refresh_service.refresh_after_auth_failure(
                    self._db,
                    self._credential,
                )
            except TokenRefreshError as exc:
                await self._session_store.delete(self._agent_session_id)
                set_span_result(span, "MCP_AUTH_FAILED", error=True)
                raise MCPTransportError(
                    "ERPNext authentication expired; please sign in again",
                    code="MCP_AUTH_FAILED",
                ) from exc

            try:
                envelope = await self._adapter.call_tool(
                    access_token=self._credential.access_token,
                    name=name,
                    arguments=arguments,
                    discover_first=discover_first,
                )
            except MCPError as exc:
                if exc.code == "MCP_AUTH_FAILED":
                    await self._session_store.delete(self._agent_session_id)
                set_span_result(span, exc.code, error=True)
                raise
            set_span_result(span, "OK")
            return envelope
