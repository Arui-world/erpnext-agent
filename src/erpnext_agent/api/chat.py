from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from agentscope.agent import Agent
from agentscope.event import (
    ReplyEndEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from agentscope.message import AssistantMsg, Msg, UserMsg
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.agents.factory import ConfiguredAgentFactory
from erpnext_agent.agents.orchestrator import Intent, IntentGate, RouteDecision
from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.agents.runtime import (
    AgentIdentityError,
    AgentRuntimeFactory,
    PreparedAgentRuntime,
)
from erpnext_agent.api.dependencies import CurrentSession, DBSession, ProtectedSession
from erpnext_agent.auth.session_store import AgentSession
from erpnext_agent.auth.token_store import (
    CredentialNotFoundError,
    StoredCredential,
    TokenStore,
)
from erpnext_agent.config import Settings
from erpnext_agent.conversations.repository import (
    ConversationMode,
    ConversationNotFoundError,
    ConversationRepository,
    StoredMessage,
)
from erpnext_agent.conversations.repository import (
    ConversationSummary as StoredConversationSummary,
)
from erpnext_agent.mcp.adapter import MCPError

router = APIRouter(prefix="/chat", tags=["chat"])

EMPTY_REPLY_FALLBACK = (
    "本次查询未能生成有效文本回复，请补充更精确的物料编码、仓库名称或查询条件后重试。"
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=16_000)
    conversation_id: UUID | None = None


class ModelChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    conversation_id: UUID | None = None


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class ConversationHistoryResponse(BaseModel):
    conversation_id: str | None
    mode: ConversationMode
    messages: list[ConversationTurn]


class CreateConversationRequest(BaseModel):
    mode: ConversationMode = "model"


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    mode: ConversationMode
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummaryResponse]


class ChatResponse(BaseModel):
    status: Literal[
        "completed",
        "denied",
        "clarification_required",
        "action_requires_persistent_approval",
    ]
    route: str
    message: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class StartedTurn:
    conversation_id: str
    messages: list[Msg]
    previous_user_messages: list[str]


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    session: CurrentSession,
    db: DBSession,
    mode: Annotated[ConversationMode, Query()] = "model",
) -> ConversationListResponse:
    settings = cast(Settings, request.app.state.settings)
    repository = ConversationRepository()
    conversations = await repository.list_owned(
        db,
        site=session.site,
        user_id=session.user_id,
        mode=mode,
        limit=settings.chat_conversation_list_limit,
    )
    return ConversationListResponse(
        conversations=[_conversation_summary(item) for item in conversations]
    )


@router.post(
    "/conversations",
    response_model=ConversationSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: CreateConversationRequest,
    session: ProtectedSession,
    db: DBSession,
) -> ConversationSummaryResponse:
    repository = ConversationRepository()
    conversation = await repository.resolve_or_create(
        db,
        conversation_id=None,
        site=session.site,
        user_id=session.user_id,
        mode=payload.mode,
    )
    await db.commit()
    return ConversationSummaryResponse(
        conversation_id=conversation.conversation_id,
        mode=payload.mode,
        title="新对话",
        message_count=0,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


@router.get("/history", response_model=ConversationHistoryResponse)
async def chat_history(
    request: Request,
    session: CurrentSession,
    db: DBSession,
    mode: Annotated[ConversationMode, Query()] = "model",
    conversation_id: Annotated[UUID | None, Query()] = None,
) -> ConversationHistoryResponse:
    settings = cast(Settings, request.app.state.settings)
    repository = ConversationRepository()
    if conversation_id is None:
        conversation = await repository.latest_owned(
            db,
            site=session.site,
            user_id=session.user_id,
            mode=mode,
        )
    else:
        conversation = await repository.get_owned(
            db,
            conversation_id=str(conversation_id),
            site=session.site,
            user_id=session.user_id,
            mode=mode,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation is None:
        return ConversationHistoryResponse(
            conversation_id=None,
            mode=mode,
            messages=[],
        )

    messages = await repository.list_messages(
        db,
        conversation_id=conversation.conversation_id,
        limit=settings.chat_history_display_limit,
    )
    return ConversationHistoryResponse(
        conversation_id=conversation.conversation_id,
        mode=mode,
        messages=[
            ConversationTurn(
                role=message.role,
                content=message.content,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )


@router.post("", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(
    payload: ChatRequest,
    request: Request,
    session: ProtectedSession,
    db: DBSession,
) -> ChatResponse:
    repository = ConversationRepository()
    turn = await _start_turn(
        payload.message,
        str(payload.conversation_id) if payload.conversation_id else None,
        "agent",
        request,
        session,
        db,
        repository,
    )
    decision = IntentGate().route_with_context(
        payload.message,
        turn.previous_user_messages,
    )
    fixed = _fixed_policy_response(decision, turn.conversation_id)
    if fixed is not None:
        await _persist_assistant(db, repository, turn.conversation_id, fixed.message)
        return fixed

    runtime = await _prepare_runtime(request, session, db)
    agent = runtime.agent_for(decision.intent)
    try:
        reply = await agent.reply(turn.messages)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Model reply failed") from exc
    text = assistant_text(reply)
    if not text:
        raise HTTPException(status_code=502, detail="Model returned no text")
    await _persist_assistant(db, repository, turn.conversation_id, text)
    return ChatResponse(
        status="completed",
        route=decision.target_agent or decision.intent.value,
        message=text,
        conversation_id=turn.conversation_id,
    )


@router.post("/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    session: ProtectedSession,
    db: DBSession,
) -> StreamingResponse:
    repository = ConversationRepository()
    turn = await _start_turn(
        payload.message,
        str(payload.conversation_id) if payload.conversation_id else None,
        "agent",
        request,
        session,
        db,
        repository,
    )
    decision = IntentGate().route_with_context(
        payload.message,
        turn.previous_user_messages,
    )
    fixed = _fixed_policy_response(decision, turn.conversation_id)
    if fixed is not None:
        await _persist_assistant(db, repository, turn.conversation_id, fixed.message)

        async def fixed_events() -> AsyncIterator[str]:
            yield _sse("conversation", {"conversation_id": turn.conversation_id})
            yield _sse("message", fixed.model_dump())
            yield _sse("done", {"status": fixed.status})

        return StreamingResponse(
            fixed_events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    runtime = await _prepare_runtime(request, session, db)
    agent = runtime.agent_for(decision.intent)
    return StreamingResponse(
        _persistent_reply_events(
            agent,
            turn,
            db,
            repository,
        ),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.post("/model/stream")
async def model_chat_stream(
    payload: ModelChatRequest,
    request: Request,
    session: ProtectedSession,
    db: DBSession,
) -> StreamingResponse:
    """Stream a tool-free model conversation with PostgreSQL-backed history."""

    repository = ConversationRepository()
    turn = await _start_turn(
        payload.message,
        str(payload.conversation_id) if payload.conversation_id else None,
        "model",
        request,
        session,
        db,
        repository,
    )
    agent_factory = cast(ConfiguredAgentFactory, request.app.state.agent_factory)
    agent = agent_factory.build_model_chat_agent()
    return StreamingResponse(
        _persistent_reply_events(
            agent,
            turn,
            db,
            repository,
        ),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


async def _start_turn(
    message: str,
    conversation_id: str | None,
    mode: ConversationMode,
    request: Request,
    session: AgentSession,
    db: AsyncSession,
    repository: ConversationRepository,
) -> StartedTurn:
    settings = cast(Settings, request.app.state.settings)
    try:
        conversation = await repository.resolve_or_create(
            db,
            conversation_id=conversation_id,
            site=session.site,
            user_id=session.user_id,
            mode=mode,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc

    history = await repository.load_context(
        db,
        conversation_id=conversation.conversation_id,
        max_messages=settings.chat_history_max_messages,
        max_chars=settings.chat_history_max_chars,
    )
    await repository.append_message(
        db,
        conversation_id=conversation.conversation_id,
        role="user",
        content=message,
    )
    await db.commit()
    return StartedTurn(
        conversation_id=conversation.conversation_id,
        messages=_conversation_messages(history, message),
        previous_user_messages=[turn.content for turn in history if turn.role == "user"],
    )


async def _persist_assistant(
    db: AsyncSession,
    repository: ConversationRepository,
    conversation_id: str,
    content: str,
) -> None:
    await repository.append_message(
        db,
        conversation_id=conversation_id,
        role="assistant",
        content=content,
    )
    await db.commit()


async def _prepare_runtime(
    request: Request,
    session: AgentSession,
    db: AsyncSession,
) -> PreparedAgentRuntime:
    credential = await _load_credential(request, session, db)
    runtime_factory = cast(
        AgentRuntimeFactory,
        request.app.state.agent_runtime_factory,
    )
    try:
        return await runtime_factory.prepare(
            access_token=credential.access_token,
            expected_user=session.user_id,
        )
    except AgentIdentityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except MCPError as exc:
        raise _mcp_http_exception(exc) from exc


async def _load_credential(
    request: Request,
    session: AgentSession,
    db: AsyncSession,
) -> StoredCredential:
    token_store = cast(TokenStore, request.app.state.token_store)
    try:
        credential: StoredCredential = await token_store.get(
            db,
            session.credential_id,
        )
    except CredentialNotFoundError as exc:
        raise HTTPException(status_code=401, detail="OAuth credential not found") from exc
    if credential.revoked_at is not None:
        raise HTTPException(status_code=401, detail="OAuth credential is revoked")
    return credential


def _mcp_http_exception(exc: MCPError) -> HTTPException:
    status_code = 401 if exc.code == "MCP_AUTH_FAILED" else 502
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc), "trace_id": exc.trace_id},
    )


def _fixed_policy_response(
    decision: RouteDecision,
    conversation_id: str,
) -> ChatResponse | None:
    if decision.intent == Intent.DENY:
        return ChatResponse(
            status="denied",
            route=decision.intent.value,
            message="当前 MVP 不支持提交、作废、删除或过账操作。",
            conversation_id=conversation_id,
        )
    if decision.intent == Intent.CLARIFY:
        return ChatResponse(
            status="clarification_required",
            route=decision.intent.value,
            message="请说明要查询、巡检，还是创建/修改哪一种 ERPNext 草稿。",
            conversation_id=conversation_id,
        )
    if decision.intent == Intent.ACTION:
        return ChatResponse(
            status="action_requires_persistent_approval",
            route=decision.intent.value,
            message="草稿写入必须先生成持久化预览和审批 Action；当前聊天运行时不会直接写入。",
            conversation_id=conversation_id,
        )
    return None


def _conversation_messages(history: list[StoredMessage], current_message: str) -> list[Msg]:
    messages: list[Msg] = []
    for turn in history:
        if turn.role == "user":
            messages.append(UserMsg(name="user", content=turn.content))
        else:
            messages.append(AssistantMsg(name="assistant", content=turn.content))
    messages.append(UserMsg(name="user", content=current_message))
    return messages


def _conversation_summary(item: StoredConversationSummary) -> ConversationSummaryResponse:
    return ConversationSummaryResponse(
        conversation_id=item.conversation_id,
        mode=item.mode,
        title=item.title,
        message_count=item.message_count,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def _persistent_reply_events(
    agent: Agent,
    turn: StartedTurn,
    db: AsyncSession,
    repository: ConversationRepository,
) -> AsyncIterator[str]:
    yield _sse("conversation", {"conversation_id": turn.conversation_id})

    async def persist_reply(content: str) -> None:
        await _persist_assistant(db, repository, turn.conversation_id, content)

    async for event in _reply_events(agent, turn.messages, on_complete=persist_reply):
        yield event


async def _reply_events(
    agent: Agent,
    inputs: str | list[Msg],
    *,
    on_complete: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncIterator[str]:
    messages: Msg | list[Msg]
    if isinstance(inputs, str):
        messages = UserMsg(name="user", content=inputs)
    else:
        messages = inputs
    text_chunks: list[str] = []
    try:
        async for event in agent.reply_stream(messages):
            if isinstance(event, TextBlockDeltaEvent):
                delta = str(event.delta)
                text_chunks.append(delta)
                yield _sse("text_delta", {"delta": delta})
            elif isinstance(event, ToolCallStartEvent):
                yield _sse(
                    "tool_call_start",
                    {
                        "tool_call_id": event.tool_call_id,
                        "tool_name": event.tool_call_name,
                    },
                )
            elif isinstance(event, ToolResultStartEvent):
                yield _sse(
                    "tool_result_start",
                    {"tool_call_id": event.tool_call_id},
                )
            elif isinstance(event, ToolResultEndEvent):
                yield _sse(
                    "tool_result_end",
                    {"tool_call_id": event.tool_call_id},
                )
            elif isinstance(event, ReplyEndEvent):
                reply_text = "".join(text_chunks).strip()
                if not reply_text:
                    reply_text = EMPTY_REPLY_FALLBACK
                    text_chunks.append(reply_text)
                    yield _sse("text_delta", {"delta": reply_text})
                if on_complete is not None:
                    await on_complete(reply_text)
                reason = getattr(event.finished_reason, "value", str(event.finished_reason))
                yield _sse("done", {"finished_reason": reason})
    except Exception:
        yield _sse("error", {"code": "AGENT_REPLY_FAILED", "message": "Agent reply failed"})


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"
