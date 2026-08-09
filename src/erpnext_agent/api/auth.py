from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from erpnext_agent.api.dependencies import CurrentSession, DBSession, ProtectedSession
from erpnext_agent.auth.oauth_client import OAuthClient, OAuthError, create_pkce_request
from erpnext_agent.auth.session_store import OAuthState, OAuthStateStore, hash_attempt_cookie
from erpnext_agent.auth.token_store import CredentialNotFoundError, TokenStore
from erpnext_agent.config import Settings
from erpnext_agent.mcp.adapter import MCPError

router = APIRouter(prefix="/auth", tags=["auth"])
OAUTH_ATTEMPT_COOKIE = "erpnext_agent_oauth_attempt"


def _safe_return_to(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value[:2048]


def _profile_data(profile: dict[str, object]) -> dict[str, object]:
    message = profile.get("message")
    return message if isinstance(message, dict) else profile


@router.get("/login")
async def login(
    request: Request,
    return_to: Annotated[str, Query(max_length=2048)] = "/",
) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    oauth: OAuthClient = request.app.state.oauth_client
    state_store: OAuthStateStore = request.app.state.oauth_state_store
    pkce = create_pkce_request()
    attempt = secrets.token_urlsafe(32)
    await state_store.put(
        pkce.state,
        OAuthState(
            verifier=pkce.verifier,
            nonce=pkce.nonce,
            attempt_hash=hash_attempt_cookie(attempt),
            return_to=_safe_return_to(return_to),
        ),
    )
    response = RedirectResponse(oauth.authorization_url(pkce), status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        OAUTH_ATTEMPT_COOKIE,
        attempt,
        max_age=settings.oauth_state_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path=settings.api_prefix + "/auth",
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    db: DBSession,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    oauth_attempt: Annotated[str | None, Cookie(alias=OAUTH_ATTEMPT_COOKIE)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    if error or not code or not state or not oauth_attempt:
        raise HTTPException(status_code=400, detail="OAuth authorization was not completed")

    settings: Settings = request.app.state.settings
    stored_state = await request.app.state.oauth_state_store.consume(state)
    if stored_state is None or not secrets.compare_digest(
        stored_state.attempt_hash,
        hash_attempt_cookie(oauth_attempt),
    ):
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")

    try:
        token = await request.app.state.oauth_client.exchange_code(
            code=code,
            verifier=stored_state.verifier,
        )
        raw_profile = await request.app.state.oauth_client.fetch_profile(token.access_token)
        profile = _profile_data(raw_profile)
        oauth_user = profile.get("email") or profile.get("sub")
        oauth_subject = profile.get("sub") or oauth_user
        if not isinstance(oauth_user, str) or not isinstance(oauth_subject, str):
            raise OAuthError("ERPNext profile did not contain a stable user identity")
        mcp_user = await request.app.state.mcp_adapter.current_user(token.access_token)
        if not secrets.compare_digest(oauth_user.casefold(), mcp_user.casefold()):
            raise OAuthError("OAuth and MCP identities do not match")
    except (OAuthError, MCPError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token_store: TokenStore = request.app.state.token_store
    credential_id = await token_store.upsert(
        db,
        site=settings.erpnext_site,
        oauth_subject=oauth_subject,
        user_id=mcp_user,
        token=token,
    )
    await db.commit()
    agent_session = await request.app.state.session_store.create(
        credential_id=credential_id,
        site=settings.erpnext_site,
        user_id=mcp_user,
    )
    response = RedirectResponse(
        settings.app_base_url + stored_state.return_to,
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        settings.session_cookie_name,
        agent_session.session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )
    response.delete_cookie(OAUTH_ATTEMPT_COOKIE, path=settings.api_prefix + "/auth")
    return response


@router.get("/session")
async def session_info(session: CurrentSession) -> dict[str, str]:
    return {
        "user": session.user_id,
        "site": session.site,
        "csrf_token": session.csrf_token,
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: ProtectedSession,
    db: DBSession,
) -> dict[str, bool]:
    settings: Settings = request.app.state.settings
    token_store: TokenStore = request.app.state.token_store
    revocation_pending = False
    try:
        credential = await token_store.get(db, session.credential_id)
        try:
            await request.app.state.oauth_client.revoke(credential.access_token)
        except OAuthError:
            revocation_pending = True
        await token_store.revoke(db, session.credential_id)
    except CredentialNotFoundError:
        pass
    await request.app.state.session_store.delete(session.session_id)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"logged_out": True, "revocation_pending": revocation_pending}
