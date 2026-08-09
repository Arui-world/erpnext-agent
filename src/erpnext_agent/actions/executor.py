from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.repository import ActionRepository, ActionStateError
from erpnext_agent.mcp.adapter import (
    ERPNextMCPAdapter,
    MCPBusinessError,
    MCPContractError,
    MCPEnvelope,
)


class ActionExecutor:
    """Executes an approved action exactly once at the Agent persistence boundary."""

    def __init__(self, repository: ActionRepository, adapter: ERPNextMCPAdapter) -> None:
        self._repository = repository
        self._adapter = adapter

    async def execute(
        self,
        session: AsyncSession,
        *,
        action: ActionRecord,
        access_token: str,
    ) -> MCPEnvelope:
        claimed = await self._repository.claim_execution(session, action.action_id)
        if not claimed:
            raise ActionStateError("Action is not approved or is already executing")
        await session.flush()

        arguments: dict[str, Any] = dict(action.canonical_arguments)
        arguments["idempotency_key"] = action.idempotency_key
        # Commit the EXECUTING transition before sending a side-effecting request. The
        # recovery worker must reuse this Action and idempotency key after a crash.
        await session.commit()
        try:
            envelope = await self._adapter.call_tool(
                access_token=access_token,
                name=action.tool_name,
                arguments=arguments,
            )
        except MCPBusinessError as exc:
            # A structured business rejection proves that the write did not succeed.
            action.status = ActionStatus.FAILED.value
            action.failure_code = exc.code
            action.failure_message = str(exc)
            action.mcp_trace_id = exc.trace_id
            await session.commit()
            raise

        data = envelope.data
        if not isinstance(data, dict):
            raise MCPContractError("Draft write returned invalid data", code="INVALID_WRITE_RESULT")
        doctype = data.get("doctype") or arguments.get("doctype")
        name = data.get("name") or arguments.get("name")
        if not isinstance(doctype, str) or not isinstance(name, str):
            raise MCPContractError(
                "Draft write returned no document identity",
                code="INVALID_WRITE_RESULT",
            )

        readback = await self._adapter.call_tool(
            access_token=access_token,
            name="erpnext_get_doc",
            arguments={"doctype": doctype, "name": name},
        )
        if not isinstance(readback.data, dict) or readback.data.get("docstatus") != 0:
            # Keep EXECUTING: a write may have happened, so recovery/manual review must
            # reconcile it with the same idempotency key instead of issuing a new write.
            raise MCPContractError("Draft readback verification failed", code="READBACK_FAILED")

        action.status = ActionStatus.SUCCEEDED.value
        action.mcp_trace_id = (
            envelope.meta.get("trace_id")
            if isinstance(envelope.meta.get("trace_id"), str)
            else None
        )
        action.result_reference = {
            "doctype": doctype,
            "name": name,
            "docstatus": 0,
            "modified": readback.data.get("modified"),
        }
        await session.commit()
        return envelope
