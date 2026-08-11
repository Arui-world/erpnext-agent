from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.coordination import acquire_action_execution_lease
from erpnext_agent.actions.executor import ActionExecutor
from erpnext_agent.actions.gateway import ActionGateway
from erpnext_agent.actions.models import ActionStatus
from erpnext_agent.actions.repository import ActionNotFoundError, ActionRepository, ActionStateError
from erpnext_agent.api.dependencies import CurrentSession, DBSession, ProtectedSession
from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.auth.token_refresh import TokenRefreshError, TokenRefreshService
from erpnext_agent.mcp.adapter import MCPBusinessError, MCPContractError, MCPError
from erpnext_agent.mcp.refreshing_caller import RefreshingMCPCaller

router = APIRouter(prefix="/approvals", tags=["approvals"])


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ActionView(BaseModel):
    action_id: str
    tool_name: str
    arguments_sha256: str
    preview: dict[str, Any]
    status: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None
    mcp_trace_id: str | None
    result_reference: dict[str, Any] | None
    failure_code: str | None
    failure_message: str | None


def _view(record: Any) -> ActionView:
    return ActionView(
        action_id=record.action_id,
        tool_name=record.tool_name,
        arguments_sha256=record.arguments_sha256,
        preview=record.preview,
        status=record.status,
        created_at=record.created_at,
        expires_at=record.expires_at,
        decided_at=record.decided_at,
        executed_at=record.executed_at,
        mcp_trace_id=record.mcp_trace_id,
        result_reference=record.result_reference,
        failure_code=record.failure_code,
        failure_message=record.failure_message,
    )


@router.get("/{action_id}", response_model=ActionView)
async def get_action(action_id: str, session: CurrentSession, db: DBSession) -> ActionView:
    try:
        record = await ActionRepository().get_for_user(
            db,
            action_id=action_id,
            site=session.site,
            user_id=session.user_id,
        )
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Action not found") from exc
    if ActionRepository.expire_if_needed(record):
        await db.commit()
    return _view(record)


@router.post("/{action_id}/decision", response_model=ActionView)
async def decide_action(
    action_id: str,
    payload: DecisionRequest,
    request: Request,
    session: ProtectedSession,
    db: DBSession,
) -> ActionView:
    gateway = ActionGateway(
        ActionRepository(),
        ttl_seconds=request.app.state.settings.action_ttl_seconds,
    )
    try:
        record = await gateway.decide(
            db,
            action_id=action_id,
            site=session.site,
            user_id=session.user_id,
            decision=payload.decision,
        )
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Action not found") from exc
    except ActionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(record)


@router.post("/{action_id}/execute", response_model=ActionView)
async def execute_action(
    action_id: str,
    request: Request,
    session: ProtectedSession,
    db: DBSession,
) -> ActionView:
    repository = ActionRepository()
    try:
        record = await repository.get_for_user(
            db,
            action_id=action_id,
            site=session.site,
            user_id=session.user_id,
        )
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Action not found") from exc

    await _validate_executable_action(repository, record, session.user_id, db)
    if record.status == ActionStatus.SUCCEEDED.value:
        return _view(record)

    try:
        execution_lease = await acquire_action_execution_lease(
            request.app.state.redis,
            action_id=action_id,
            ttl_seconds=request.app.state.settings.action_execution_lock_ttl_seconds,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ACTION_COORDINATION_UNAVAILABLE",
                "message": "Action execution coordination is unavailable",
            },
        ) from exc
    if execution_lease is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACTION_EXECUTION_BUSY",
                "message": "This Action is already being executed or reconciled",
            },
        )

    try:
        # Re-read after obtaining the distributed lock. Another instance may have
        # completed the Action between the first ownership check and lock acquisition.
        record = await repository.get_for_user(
            db,
            action_id=action_id,
            site=session.site,
            user_id=session.user_id,
        )
        await _validate_executable_action(repository, record, session.user_id, db)
        if record.status == ActionStatus.SUCCEEDED.value:
            return _view(record)

        refresh_service: TokenRefreshService = request.app.state.token_refresh_service
        session_store: SessionStore = request.app.state.session_store
        credential = await refresh_service.get_valid(db, session.credential_id)
        caller = RefreshingMCPCaller(
            adapter=request.app.state.mcp_adapter,
            refresh_service=refresh_service,
            db=db,
            credential=credential,
            session_store=session_store,
            agent_session_id=session.session_id,
        )
        identity = await caller.call_tool(
            access_token=credential.access_token,
            name="erpnext_get_current_user",
            arguments={},
        )
        user = identity.data.get("user") if isinstance(identity.data, dict) else None
        if not isinstance(user, str):
            raise MCPContractError(
                "Current-user response has no user",
                code="INVALID_IDENTITY",
            )
        if not secrets.compare_digest(user.casefold(), session.user_id.casefold()):
            raise HTTPException(
                status_code=403,
                detail="Agent session and ERPNext MCP identities do not match",
            )
        executor = ActionExecutor(repository, caller)
        if record.status == ActionStatus.EXECUTING.value:
            await executor.reconcile(
                db,
                action=record,
                access_token=credential.access_token,
            )
        else:
            await executor.execute(
                db,
                action=record,
                access_token=credential.access_token,
            )
    except TokenRefreshError as exc:
        await request.app.state.session_store.delete(session.session_id)
        raise HTTPException(
            status_code=401,
            detail={"code": "REAUTHENTICATION_REQUIRED", "message": str(exc)},
        ) from exc
    except HTTPException:
        raise
    except MCPBusinessError as exc:
        if exc.code == "PERMISSION_DENIED":
            status_code = 403
        elif exc.code == "VERSION_CONFLICT":
            status_code = 409
        else:
            status_code = 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc), "trace_id": exc.trace_id},
        ) from exc
    except MCPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": exc.code, "message": str(exc), "trace_id": exc.trace_id},
        ) from exc
    except ActionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        await execution_lease.release()
    return _view(record)


async def _validate_executable_action(
    repository: ActionRepository,
    record: Any,
    user_id: str,
    db: AsyncSession,
) -> None:
    if repository.expire_if_needed(record):
        await db.commit()
        raise HTTPException(status_code=409, detail="Action approval has expired")
    if record.status == ActionStatus.SUCCEEDED.value:
        return
    if record.status not in {
        ActionStatus.APPROVED.value,
        ActionStatus.EXECUTING.value,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Action cannot execute from status {record.status}",
        )
    if record.decided_by != user_id:
        raise HTTPException(status_code=403, detail="Action decision identity mismatch")
