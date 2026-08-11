from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from erpnext_agent import __version__
from erpnext_agent.agents.factory import ConfiguredAgentFactory
from erpnext_agent.agents.runtime import AgentRuntimeFactory
from erpnext_agent.api import approvals, auth, chat, health
from erpnext_agent.auth.oauth_client import OAuthClient
from erpnext_agent.auth.session_store import OAuthStateStore, SessionStore
from erpnext_agent.auth.token_store import TokenStore
from erpnext_agent.config import Settings, get_settings
from erpnext_agent.db import create_engine, create_schema, create_session_factory
from erpnext_agent.mcp.adapter import ERPNextMCPAdapter


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    web_root = Path(__file__).resolve().parent / "web"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved.database_url, echo=resolved.app_debug)
        session_factory = create_session_factory(engine)
        redis = Redis.from_url(resolved.redis_url.get_secret_value(), decode_responses=True)
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(resolved.mcp_http_timeout_seconds),
            follow_redirects=False,
        )

        app.state.settings = resolved
        app.state.db_engine = engine
        app.state.db_session_factory = session_factory
        app.state.redis = redis
        app.state.http = http
        app.state.session_store = SessionStore(
            redis,
            resolved.session_ttl_seconds,
            resolved.session_secret.get_secret_value(),
        )
        app.state.oauth_state_store = OAuthStateStore(redis, resolved.oauth_state_ttl_seconds)
        app.state.oauth_client = OAuthClient(resolved, http)
        app.state.token_store = TokenStore(
            encryption_key=resolved.token_encryption_key.get_secret_value(),
            key_version=resolved.token_encryption_key_version,
            client_id=resolved.oauth_client_id,
        )
        app.state.mcp_adapter = ERPNextMCPAdapter(
            url=resolved.effective_mcp_url,
            http=http,
            verify_contract=resolved.mcp_verify_tool_contract,
            host_header=resolved.erpnext_host_header,
        )
        app.state.agent_factory = ConfiguredAgentFactory(resolved)
        app.state.agent_runtime_factory = AgentRuntimeFactory(
            agent_factory=app.state.agent_factory,
            adapter=app.state.mcp_adapter,
        )
        if resolved.auto_create_schema:
            await create_schema(engine)
        try:
            yield
        finally:
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title=resolved.app_name,
        version=__version__,
        debug=resolved.app_debug,
        lifespan=lifespan,
        docs_url="/docs" if resolved.app_env != "production" else None,
        redoc_url=None,
    )
    app.state.settings = resolved
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=resolved.trusted_host_list)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(auth.router, prefix=resolved.api_prefix)
    app.include_router(chat.router, prefix=resolved.api_prefix)
    app.include_router(approvals.router, prefix=resolved.api_prefix)

    app.mount("/assets", StaticFiles(directory=web_root), name="web-assets")

    @app.get("/", include_in_schema=False)
    async def chat_ui() -> FileResponse:
        return FileResponse(web_root / "index.html")

    return app


app = create_app()
