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
        try:
            return await self._adapter.call_tool(
                access_token=self._credential.access_token,
                name=name,
                arguments=arguments,
                discover_first=discover_first,
            )
        except MCPError as exc:
            if exc.code != "MCP_AUTH_FAILED":
                raise

        try:
            self._credential = await self._refresh_service.refresh_after_auth_failure(
                self._db,
                self._credential,
            )
        except TokenRefreshError as exc:
            await self._session_store.delete(self._agent_session_id)
            raise MCPTransportError(
                "ERPNext authentication expired; please sign in again",
                code="MCP_AUTH_FAILED",
            ) from exc

        try:
            return await self._adapter.call_tool(
                access_token=self._credential.access_token,
                name=name,
                arguments=arguments,
                discover_first=discover_first,
            )
        except MCPError as exc:
            if exc.code == "MCP_AUTH_FAILED":
                await self._session_store.delete(self._agent_session_id)
            raise
