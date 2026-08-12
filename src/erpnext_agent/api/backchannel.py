from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError

from erpnext_agent.actions.repository import ActionRepository
from erpnext_agent.api.dependencies import DBSession
from erpnext_agent.auth.authorization import AuthorizationService
from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.auth.token_store import CredentialNotFoundError, TokenStore
from erpnext_agent.config import Settings

router = APIRouter(prefix="/auth", tags=["auth"])


class BackchannelLogoutEvent(BaseModel):
    event_id: UUID
    binding_id: UUID
    site: str = Field(min_length=1, max_length=255)
    user: str = Field(min_length=1, max_length=255)
    timestamp: int


@router.post("/backchannel-logout")
async def backchannel_logout(
    request: Request,
    db: DBSession,
    timestamp_header: Annotated[str | None, Header(alias="X-Agent-Timestamp")] = None,
    signature_header: Annotated[str | None, Header(alias="X-Agent-Signature")] = None,
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    secret = settings.erp_logout_webhook_secret
    if secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERPNext logout webhook is not configured",
        )
    raw = await request.body()
    if len(raw) > 8_192:
        raise HTTPException(status_code=413, detail="Logout event is too large")
    if timestamp_header is None or signature_header is None:
        raise HTTPException(status_code=401, detail="Logout event signature is missing")
    try:
        timestamp = int(timestamp_header)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Logout event timestamp is invalid") from exc
    if abs(int(time.time()) - timestamp) > settings.oauth_logout_event_max_skew_seconds:
        raise HTTPException(status_code=401, detail="Logout event timestamp is outside the window")
    expected = hmac.new(
        secret.get_secret_value().encode(),
        timestamp_header.encode() + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    supplied = signature_header.removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Logout event signature is invalid")
    try:
        event = BackchannelLogoutEvent.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail="Logout event payload is invalid") from exc
    if event.timestamp != timestamp or event.site != settings.erpnext_site:
        raise HTTPException(status_code=400, detail="Logout event site or timestamp is invalid")

    redis = request.app.state.redis
    replay_key = f"agent:logout-event:{event.event_id}"
    reserved = await redis.set(replay_key, "processing", ex=60, nx=True)
    if not reserved:
        return {"accepted": True, "duplicate": True}
    try:
        binding_id = str(event.binding_id)
        token_store: TokenStore = request.app.state.token_store
        session_store: SessionStore = request.app.state.session_store
        credential = None
        try:
            credential = await token_store.get_for_binding(db, binding_id=binding_id)
        except CredentialNotFoundError:
            # Unknown bindings are acknowledged to keep ERPNext's outbox idempotent.
            pass
        if credential is not None and credential.user_id.casefold() != event.user.casefold():
            raise HTTPException(status_code=409, detail="Logout binding identity mismatch")
        await token_store.revoke_by_binding(db, binding_id)
        session_ids = await session_store.delete_by_binding(binding_id)
        await ActionRepository().expire_for_sessions(db, session_ids=session_ids)
        authorization: AuthorizationService = request.app.state.authorization_service
        await authorization.invalidate_binding_cache(binding_id)
        await db.commit()
        await redis.setex(
            replay_key,
            settings.oauth_logout_event_replay_ttl_seconds,
            "completed",
        )
    except Exception:
        await db.rollback()
        await redis.delete(replay_key)
        raise
    return {"accepted": True, "duplicate": False, "sessions_revoked": len(session_ids)}
