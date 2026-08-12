from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request, status
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.auth.authorization import (
    AuthorizationRevokedError,
    AuthorizationService,
    AuthorizationUnavailableError,
)
from erpnext_agent.auth.session_store import AgentSession, SessionStore
from erpnext_agent.config import Settings
from erpnext_agent.observability import Telemetry


def settings_from_request(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def database_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def current_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(database_session)],
) -> AgentSession:
    settings: Settings = request.app.state.settings
    store: SessionStore = request.app.state.session_store
    raw_id = request.cookies.get(settings.session_cookie_name)
    if not raw_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    session = await store.get(raw_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    telemetry: Telemetry = request.app.state.telemetry
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("session_id", telemetry.identifier(session.session_id))
    authorization: AuthorizationService = request.app.state.authorization_service
    try:
        await authorization.validate(db, session)
    except AuthorizationRevokedError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ERP_AUTHORIZATION_REVOKED", "message": str(exc)},
        ) from exc
    except AuthorizationUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ERP_AUTHORIZATION_UNAVAILABLE", "message": str(exc)},
        ) from exc
    return session


async def csrf_protected_session(
    session: Annotated[AgentSession, Depends(current_session)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AgentSession:
    if csrf_token is None or not secrets.compare_digest(csrf_token, session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
    return session


DBSession = Annotated[AsyncSession, Depends(database_session)]
CurrentSession = Annotated[AgentSession, Depends(current_session)]
ProtectedSession = Annotated[AgentSession, Depends(csrf_protected_session)]
