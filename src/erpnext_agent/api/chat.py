from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from erpnext_agent.agents.orchestrator import Intent, IntentGate
from erpnext_agent.api.dependencies import ProtectedSession

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=16_000)


class ChatResponse(BaseModel):
    status: Literal["denied", "clarification_required", "runtime_not_configured"]
    route: str
    message: str


@router.post("", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(payload: ChatRequest, session: ProtectedSession) -> ChatResponse:
    """Expose the deterministic gate while the model/ToolBase bridge is not wired."""

    decision = IntentGate().route(payload.message)
    if decision.intent == Intent.DENY:
        return ChatResponse(
            status="denied",
            route=decision.intent.value,
            message="当前 MVP 不支持提交、作废、删除或过账操作。",
        )
    if decision.intent == Intent.CLARIFY:
        return ChatResponse(
            status="clarification_required",
            route=decision.intent.value,
            message="请说明要查询、巡检，还是创建/修改哪一种 ERPNext 草稿。",
        )
    return ChatResponse(
        status="runtime_not_configured",
        route=decision.target_agent or decision.intent.value,
        message="请求已通过策略门控；模型与 AgentScope ToolBase 运行时将在下一阶段接入。",
    )

