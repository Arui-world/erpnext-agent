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
from erpnext_agent.actions.recovery import ActionRecoveryWorker
from erpnext_agent.actions.repository import ActionRepository
from erpnext_agent.agents.factory import ConfiguredAgentFactory
from erpnext_agent.agents.runtime import AgentRuntimeFactory
from erpnext_agent.api import approvals, auth, backchannel, chat, health
from erpnext_agent.auth.authorization import AuthorizationService
from erpnext_agent.auth.oauth_client import OAuthClient
from erpnext_agent.auth.session_store import OAuthStateStore, SessionStore
from erpnext_agent.auth.token_refresh import TokenRefreshService
from erpnext_agent.auth.token_store import TokenStore
from erpnext_agent.config import Settings, get_settings
from erpnext_agent.conversations.memory import (
    AgentSummaryGenerator,
    ConversationMemoryPolicy,
    ConversationMemoryService,
)
from erpnext_agent.conversations.repository import ConversationRepository
from erpnext_agent.conversations.retention import (
    ConversationRetentionPolicy,
    ConversationRetentionWorker,
)
from erpnext_agent.db import (
    create_engine,
    create_session_factory,
    verify_database_revision,
)
from erpnext_agent.mcp.adapter import ERPNextMCPAdapter
from erpnext_agent.observability import (
    Telemetry,
    create_telemetry,
    normalized_request_id,
    set_span_result,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    web_root = Path(__file__).resolve().parent / "web"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        telemetry = create_telemetry(resolved)
        app.state.telemetry = telemetry
        engine = create_engine(resolved.database_url, echo=resolved.app_debug)
        try:
            database_revision = await verify_database_revision(engine)
        except Exception:
            await engine.dispose()
            telemetry.shutdown()
            raise
        session_factory = create_session_factory(engine)
        redis = Redis.from_url(resolved.redis_url.get_secret_value(), decode_responses=True)
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(resolved.mcp_http_timeout_seconds),
            follow_redirects=False,
        )

        app.state.settings = resolved
        app.state.db_engine = engine
        app.state.database_revision = database_revision
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
        app.state.token_refresh_service = TokenRefreshService(
            store=app.state.token_store,
            oauth=app.state.oauth_client,
            redis=redis,
            leeway_seconds=resolved.oauth_refresh_leeway_seconds,
            lock_ttl_seconds=resolved.oauth_refresh_lock_ttl_seconds,
            wait_seconds=resolved.oauth_refresh_wait_seconds,
            poll_seconds=resolved.oauth_refresh_poll_seconds,
        )
        app.state.authorization_service = AuthorizationService(
            oauth=app.state.oauth_client,
            refresh=app.state.token_refresh_service,
            token_store=app.state.token_store,
            session_store=app.state.session_store,
            redis=redis,
            client_id=resolved.oauth_client_id,
            cache_seconds=resolved.oauth_introspection_cache_seconds,
        )
        app.state.mcp_adapter = ERPNextMCPAdapter(
            url=resolved.effective_mcp_url,
            http=http,
            verify_contract=resolved.mcp_verify_tool_contract,
            host_header=resolved.erpnext_host_header,
            telemetry=telemetry,
        )
        app.state.agent_factory = ConfiguredAgentFactory(resolved)
        app.state.conversation_memory_service = ConversationMemoryService(
            redis=redis,
            generator=AgentSummaryGenerator(app.state.agent_factory, telemetry),
            policy=ConversationMemoryPolicy(
                enabled=resolved.chat_summary_enabled,
                trigger_messages=resolved.chat_summary_trigger_messages,
                trigger_chars=resolved.chat_summary_trigger_chars,
                keep_recent_messages=resolved.chat_summary_keep_recent_messages,
                source_max_chars=resolved.chat_summary_source_max_chars,
                summary_max_chars=resolved.chat_summary_max_chars,
                lock_ttl_seconds=resolved.chat_summary_lock_ttl_seconds,
            ),
        )
        app.state.agent_runtime_factory = AgentRuntimeFactory(
            agent_factory=app.state.agent_factory,
            adapter=app.state.mcp_adapter,
        )
        app.state.action_recovery_worker = ActionRecoveryWorker(
            enabled=resolved.action_recovery_enabled,
            session_factory=session_factory,
            redis=redis,
            repository=ActionRepository(),
            token_store=app.state.token_store,
            refresh_service=app.state.token_refresh_service,
            session_store=app.state.session_store,
            adapter=app.state.mcp_adapter,
            poll_seconds=resolved.action_recovery_poll_seconds,
            retry_seconds=resolved.action_recovery_retry_seconds,
            batch_size=resolved.action_recovery_batch_size,
            execution_lock_ttl_seconds=resolved.action_execution_lock_ttl_seconds,
            telemetry=telemetry,
        )
        app.state.conversation_retention_worker = ConversationRetentionWorker(
            session_factory=session_factory,
            repository=ConversationRepository(),
            policy=ConversationRetentionPolicy(
                enabled=resolved.chat_retention_enabled,
                retention_days=resolved.chat_retention_days,
                deleted_retention_days=resolved.chat_deleted_retention_days,
                empty_retention_hours=resolved.chat_empty_retention_hours,
                sweep_seconds=resolved.chat_retention_sweep_seconds,
                batch_size=resolved.chat_retention_batch_size,
            ),
            telemetry=telemetry,
        )
        app.state.action_recovery_worker.start()
        app.state.conversation_retention_worker.start()
        try:
            yield
        finally:
            await app.state.conversation_retention_worker.stop()
            await app.state.action_recovery_worker.stop()
            await http.aclose()
            await redis.aclose()
            await engine.dispose()
            telemetry.shutdown()

    app = FastAPI(
        title=resolved.app_name,
        version=__version__,
        debug=resolved.app_debug,
        lifespan=lifespan,
        docs_url="/docs" if resolved.app_env != "production" else None,
        redoc_url=None,
    )
    app.state.settings = resolved
    app.state.telemetry = Telemetry.disabled(
        resolved.session_secret.get_secret_value()
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=resolved.trusted_host_list)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = normalized_request_id(
            request.headers.get("X-Request-ID"),
            fallback=str(uuid.uuid4()),
        )
        request.state.request_id = request_id
        telemetry = request.app.state.telemetry
        parent_context = telemetry.extract(request.headers)
        with telemetry.span(
            "http.request",
            parent_context=parent_context,
            attributes={
                "request_id": request_id,
                "http.request.method": request.method,
            },
        ) as span:
            try:
                response = await call_next(request)
            except Exception:
                set_span_result(span, "HTTP_UNHANDLED_EXCEPTION", error=True)
                raise
            route = request.scope.get("route")
            route_path = getattr(route, "path", None)
            if isinstance(route_path, str):
                span.set_attribute("http.route", route_path)
            span.set_attribute("http.response.status_code", response.status_code)
            set_span_result(
                span,
                f"HTTP_{response.status_code}",
                error=response.status_code >= 500,
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(auth.router, prefix=resolved.api_prefix)
    app.include_router(backchannel.router, prefix=resolved.api_prefix)
    app.include_router(chat.router, prefix=resolved.api_prefix)
    app.include_router(approvals.router, prefix=resolved.api_prefix)

    app.mount("/assets", StaticFiles(directory=web_root), name="web-assets")

    @app.get("/", include_in_schema=False)
    async def chat_ui() -> FileResponse:
        return FileResponse(web_root / "index.html")

    return app


app = create_app()
