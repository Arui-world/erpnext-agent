from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from erpnext_agent.agents.model_factory import model_configuration_is_complete

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live(request: Request) -> dict[str, str]:
    return {"status": "ok", "service": request.app.state.settings.app_name}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception:  # readiness must return a stable response, not connection details
        checks["redis"] = "error"

    try:
        async with request.app.state.db_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    recovery_worker = request.app.state.action_recovery_worker
    if recovery_worker.enabled:
        checks["action_recovery"] = "ok" if recovery_worker.running else "error"

    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if healthy else "not_ready",
        "checks": checks,
        "capabilities": {
            "oauth": True,
            "oauth_auto_refresh": True,
            "approval_persistence": True,
            "action_approval_execution": True,
            "action_recovery_worker": (
                recovery_worker.enabled and recovery_worker.running
            ),
            "conversation_auto_summary": request.app.state.settings.chat_summary_enabled,
            "model_configured": model_configuration_is_complete(request.app.state.settings),
            "agent_chat_runtime": model_configuration_is_complete(
                request.app.state.settings
            ),
        },
    }
