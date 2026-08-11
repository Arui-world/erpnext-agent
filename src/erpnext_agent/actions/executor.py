from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.gateway import canonicalize_arguments
from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.repository import ActionRepository, ActionStateError
from erpnext_agent.mcp.adapter import (
    MCPBusinessError,
    MCPContractError,
    MCPEnvelope,
    MCPError,
)
from erpnext_agent.mcp.tool_bridge import MCPToolCaller


class ActionExecutor:
    """Executes an approved action exactly once at the Agent persistence boundary."""

    _UNCERTAIN_BUSINESS_CODES = frozenset(
        {"IDEMPOTENCY_IN_PROGRESS", "IDEMPOTENCY_UNAVAILABLE"}
    )

    def __init__(self, repository: ActionRepository, caller: MCPToolCaller) -> None:
        self._repository = repository
        self._caller = caller

    async def execute(
        self,
        session: AsyncSession,
        *,
        action: ActionRecord,
        access_token: str,
    ) -> MCPEnvelope:
        self._validate_arguments_digest(action)
        claimed = await self._repository.claim_execution(session, action.action_id)
        if not claimed:
            raise ActionStateError("Action is not approved or is already executing")
        await session.flush()

        # Commit the EXECUTING transition before sending a side-effecting request. The
        # recovery worker must reuse this Action and idempotency key after a crash.
        await session.commit()
        return await self._write_and_verify(
            session,
            action=action,
            access_token=access_token,
        )

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        action: ActionRecord,
        access_token: str,
    ) -> MCPEnvelope:
        """Retry an uncertain EXECUTING Action with its original key and parameters."""

        self._validate_arguments_digest(action)
        if action.status != ActionStatus.EXECUTING.value:
            raise ActionStateError("Only an EXECUTING Action can be reconciled")
        return await self._write_and_verify(
            session,
            action=action,
            access_token=access_token,
        )

    async def _write_and_verify(
        self,
        session: AsyncSession,
        *,
        action: ActionRecord,
        access_token: str,
    ) -> MCPEnvelope:
        arguments: dict[str, Any] = dict(action.canonical_arguments)
        arguments["idempotency_key"] = action.idempotency_key
        try:
            envelope = await self._caller.call_tool(
                access_token=access_token,
                name=action.tool_name,
                arguments=arguments,
            )
        except MCPBusinessError as exc:
            if exc.code in self._UNCERTAIN_BUSINESS_CODES:
                await self._record_uncertain_failure(session, action, exc)
                raise
            # A structured business rejection proves that the write did not succeed.
            action.status = ActionStatus.FAILED.value
            action.failure_code = exc.code
            action.failure_message = str(exc)
            action.mcp_trace_id = exc.trace_id
            await session.commit()
            raise
        except MCPError as exc:
            await self._record_uncertain_failure(session, action, exc)
            raise

        data = envelope.data
        if not isinstance(data, dict):
            contract_error = MCPContractError(
                "Draft write returned invalid data",
                code="INVALID_WRITE_RESULT",
            )
            await self._record_uncertain_failure(session, action, contract_error)
            raise contract_error
        doctype = data.get("doctype") or arguments.get("doctype")
        name = data.get("name") or arguments.get("name")
        if not isinstance(doctype, str) or not isinstance(name, str):
            contract_error = MCPContractError(
                "Draft write returned no document identity",
                code="INVALID_WRITE_RESULT",
            )
            await self._record_uncertain_failure(session, action, contract_error)
            raise contract_error

        try:
            readback = await self._caller.call_tool(
                access_token=access_token,
                name="erpnext_get_doc",
                arguments={"doctype": doctype, "name": name},
            )
        except MCPError as exc:
            await self._record_uncertain_failure(session, action, exc)
            raise
        if not isinstance(readback.data, dict) or readback.data.get("docstatus") != 0:
            # Keep EXECUTING: a write may have happened, so recovery/manual review must
            # reconcile it with the same idempotency key instead of issuing a new write.
            contract_error = MCPContractError(
                "Draft readback verification failed",
                code="READBACK_FAILED",
            )
            await self._record_uncertain_failure(session, action, contract_error)
            raise contract_error

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
        action.executed_at = datetime.now(UTC)
        action.failure_code = None
        action.failure_message = None
        await session.commit()
        return envelope

    @staticmethod
    def _validate_arguments_digest(action: ActionRecord) -> None:
        _, current_digest = canonicalize_arguments(dict(action.canonical_arguments))
        if not secrets.compare_digest(current_digest, action.arguments_sha256):
            raise ActionStateError("Action arguments no longer match the approved preview")

    @staticmethod
    async def _record_uncertain_failure(
        session: AsyncSession,
        action: ActionRecord,
        exc: MCPError,
    ) -> None:
        action.failure_code = exc.code
        action.failure_message = str(exc)
        action.mcp_trace_id = exc.trace_id
        await session.commit()
