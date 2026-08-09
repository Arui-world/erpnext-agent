from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from erpnext_agent.actions.gateway import ActionGateway
from erpnext_agent.actions.repository import ActionNotFoundError, ActionRepository, ActionStateError
from erpnext_agent.api.dependencies import CurrentSession, DBSession, ProtectedSession

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
