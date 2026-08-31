from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
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
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.gateway import ActionGateway
from erpnext_agent.actions.proposal import (
    ActionProposalService,
    ActionProposalTool,
    action_public_payload,
    action_summary_markdown,
)
from erpnext_agent.actions.repository import ActionRepository
from erpnext_agent.agents.factory import ConfiguredAgentFactory
from erpnext_agent.agents.intent_classifier import IntentClassifier
from erpnext_agent.agents.orchestrator import Intent, RouteDecision
from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.agents.runtime import (
    AgentIdentityError,
    AgentRuntimeFactory,
    PreparedAgentRuntime,
)
from erpnext_agent.api.dependencies import CurrentSession, DBSession, ProtectedSession
from erpnext_agent.auth.session_store import AgentSession, SessionStore
from erpnext_agent.auth.token_refresh import TokenRefreshError, TokenRefreshService
from erpnext_agent.config import Settings
from erpnext_agent.conversations.memory import ConversationMemoryService
from erpnext_agent.conversations.repository import (
    ConversationMode,
    ConversationNotFoundError,
    ConversationRepository,
    StoredMessage,
    normalize_conversation_title,
)
from erpnext_agent.conversations.repository import (
    ConversationSummary as StoredConversationSummary,
)
from erpnext_agent.mcp.adapter import MCPError
from erpnext_agent.mcp.refreshing_caller import RefreshingMCPCaller
from erpnext_agent.observability import Telemetry, set_span_result

router = APIRouter(prefix="/chat", tags=["chat"])

logger = logging.getLogger(__name__)

EMPTY_REPLY_FALLBACK = (
    "本次查询未能生成有效文本回复，请补充更精确的物料编码、仓库名称或查询条件后重试。"
)
TURN_TIMEOUT_FALLBACK = (
    "本回合在时间预算内未完成，上述部分查询结果未能整理为最终答复；"
    "请缩小问题范围或稍后重试。"
)
UNROUNDED_REPLY_WARNING = (
    "\n\n⚠ 本回合未获得任何 ERPNext 工具结果，以上包含数字的内容未经查询验证，"
    "请重试或补充范围条件。"
)
_NUMERIC_CLAIM_RE = re.compile(r"\d")


def _contains_numeric_claim(text: str) -> bool:
    return bool(_NUMERIC_CLAIM_RE.search(text))


def _context_has_tool_result(agent: Agent) -> bool:
    for message in getattr(agent.state, "context", []):
        try:
            if message.get_content_blocks("tool_result"):
                return True
        except (AttributeError, TypeError):
            continue
    return False


def _turn_timeout_seconds(intent: Intent, settings: Settings) -> float:
    if intent == Intent.PATROL:
        return settings.patrol_turn_timeout_seconds
    return settings.agent_turn_timeout_seconds


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


class RenameConversationRequest(BaseModel):
    mode: ConversationMode
    title: str = Field(min_length=1, max_length=80)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Conversation title must be a string")
        return normalize_conversation_title(value)


class ConversationUpdateResponse(BaseModel):
    conversation_id: str
    mode: ConversationMode
    title: str
    updated_at: datetime


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
        "action_pending_approval",
    ]
    route: str
    message: str
    conversation_id: str
    action: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StartedTurn:
    conversation_id: str
    turn_id: str
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


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationUpdateResponse,
)
async def rename_conversation(
    conversation_id: UUID,
    payload: RenameConversationRequest,
    session: ProtectedSession,
    db: DBSession,
) -> ConversationUpdateResponse:
    repository = ConversationRepository()
    try:
        conversation = await repository.rename_owned(
            db,
            conversation_id=str(conversation_id),
            site=session.site,
            user_id=session.user_id,
            mode=payload.mode,
            title=payload.title,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    await db.commit()
    return ConversationUpdateResponse(
        conversation_id=conversation.conversation_id,
        mode=payload.mode,
        title=conversation.title or "新对话",
        updated_at=conversation.updated_at,
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_conversation(
    conversation_id: UUID,
    session: ProtectedSession,
    db: DBSession,
    mode: Annotated[ConversationMode, Query()] = "model",
) -> Response:
    repository = ConversationRepository()
    try:
        await repository.soft_delete_owned(
            db,
            conversation_id=str(conversation_id),
            site=session.site,
            user_id=session.user_id,
            mode=mode,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    decision = await _classify_route(request, payload.message, turn.previous_user_messages)
    fixed = _fixed_policy_response(decision, turn.conversation_id)
    if fixed is not None:
        await _persist_assistant(db, repository, turn.conversation_id, fixed.message)
        return fixed

    runtime = await _prepare_runtime(
        request,
        session,
        db,
        action_conversation_id=(
            turn.conversation_id if decision.intent == Intent.ACTION else None
        ),
    )
    agent = runtime.agent_for(decision.intent)
    agent_messages = turn.messages
    if decision.intent == Intent.ACTION:
        agent_messages = [
            AssistantMsg(
                name="runtime_action_context",
                content=(
                    "运行时规则：仓库字段可以使用用户提供的简称，例如‘仓库’。这不是缺失参数，"
                    "不得要求用户补充‘仓库 - 公司简称’。直接调用 erpnext_propose_draft_action；"
                    "服务端会按当前用户权限解析唯一完整 Warehouse 名称。"
                ),
            ),
            *turn.messages,
        ]
    telemetry = cast(Telemetry, request.app.state.telemetry)
    settings = cast(Settings, request.app.state.settings)
    with telemetry.span(
        "agent.model.reply",
        attributes={
            "turn_id": turn.turn_id,
            "agent_name": agent.name,
            "model_call_id": str(uuid.uuid4()),
        },
    ) as span:
        try:
            async with asyncio.timeout(_turn_timeout_seconds(decision.intent, settings)):
                reply = await agent.reply(agent_messages)
        except TimeoutError as exc:
            set_span_result(span, "AGENT_TURN_TIMEOUT", error=True)
            try:
                await _persist_assistant(
                    db, repository, turn.conversation_id, TURN_TIMEOUT_FALLBACK
                )
            except Exception:  # persistence failure must not mask the timeout
                logger.warning(
                    "Failed to persist timeout fallback for conversation %s",
                    turn.conversation_id,
                )
            raise HTTPException(status_code=504, detail="Agent turn timed out") from exc
        except Exception as exc:
            set_span_result(span, "MODEL_REPLY_FAILED", error=True)
            raise HTTPException(status_code=502, detail="Model reply failed") from exc
        text = assistant_text(reply)
        if not text:
            set_span_result(span, "MODEL_EMPTY_REPLY", error=True)
            raise HTTPException(status_code=502, detail="Model returned no text")
        if _contains_numeric_claim(text) and not _context_has_tool_result(agent):
            # Same ungrounded-number guard as the streaming path: the final
            # reply Msg is text-only even when tools ran, so grounding is
            # checked against the agent's context, not the reply content.
            text += UNROUNDED_REPLY_WARNING
        set_span_result(span, "OK")
    action = (
        runtime.action_proposal_tool.record
        if runtime.action_proposal_tool is not None
        else None
    )
    if action is not None:
        text += action_summary_markdown(action)
    await _persist_assistant(db, repository, turn.conversation_id, text)
    return ChatResponse(
        status="action_pending_approval" if action is not None else "completed",
        route=decision.target_agent or decision.intent.value,
        message=text,
        conversation_id=turn.conversation_id,
        action=action_public_payload(action) if action is not None else None,
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
    decision = await _classify_route(request, payload.message, turn.previous_user_messages)
    fixed = _fixed_policy_response(decision, turn.conversation_id)
    if fixed is not None:
        await _persist_assistant(db, repository, turn.conversation_id, fixed.message)

        async def fixed_events() -> AsyncIterator[str]:
            yield _sse("conversation", {"conversation_id": turn.conversation_id})
            yield _sse("route", {"route": decision.target_agent or decision.intent.value})
            yield _sse("message", fixed.model_dump())
            yield _sse("done", {"status": fixed.status})

        return StreamingResponse(
            fixed_events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    runtime = await _prepare_runtime(
        request,
        session,
        db,
        action_conversation_id=(
            turn.conversation_id if decision.intent == Intent.ACTION else None
        ),
    )
    agent = runtime.agent_for(decision.intent)
    telemetry = cast(Telemetry, request.app.state.telemetry)
    settings = cast(Settings, request.app.state.settings)

    async def routed_events() -> AsyncIterator[str]:
        yield _sse("route", {"route": decision.target_agent or decision.intent.value})
        async for event in _persistent_reply_events(
            agent,
            turn,
            db,
            repository,
            action_proposal_tool=runtime.action_proposal_tool,
            telemetry=telemetry,
            parent_context=telemetry.current_context(),
            timeout_seconds=_turn_timeout_seconds(decision.intent, settings),
            require_grounding=True,
        ):
            yield event

    return StreamingResponse(
        routed_events(),
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
    telemetry = cast(Telemetry, request.app.state.telemetry)
    settings = cast(Settings, request.app.state.settings)
    return StreamingResponse(
        _persistent_reply_events(
            agent,
            turn,
            db,
            repository,
            telemetry=telemetry,
            parent_context=telemetry.current_context(),
            timeout_seconds=settings.agent_turn_timeout_seconds,
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

    memory_service = cast(
        ConversationMemoryService,
        request.app.state.conversation_memory_service,
    )
    context = await memory_service.prepare_context(
        db,
        conversation_id=conversation.conversation_id,
        current_message=message,
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
        turn_id=str(uuid.uuid4()),
        messages=_conversation_messages(
            context.messages,
            message,
            summary=context.summary,
        ),
        previous_user_messages=[
            turn.content for turn in context.messages if turn.role == "user"
        ],
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


async def _classify_route(
    request: Request,
    message: str,
    previous_user_messages: list[str],
) -> RouteDecision:
    """Classify natural language while retaining the deterministic safety fallback."""

    agent_factory = cast(ConfiguredAgentFactory, request.app.state.agent_factory)
    settings = cast(Settings, request.app.state.settings)
    return await IntentClassifier(
        agent_factory.model,
        timeout_seconds=settings.intent_classifier_timeout_seconds,
    ).classify(message, previous_user_messages)


async def _prepare_runtime(
    request: Request,
    session: AgentSession,
    db: AsyncSession,
    *,
    action_conversation_id: str | None = None,
) -> PreparedAgentRuntime:
    refresh_service = cast(
        TokenRefreshService,
        request.app.state.token_refresh_service,
    )
    try:
        credential = await refresh_service.get_valid(db, session.credential_id)
    except TokenRefreshError as exc:
        await _expire_agent_session(request, session)
        raise _reauthentication_required(exc) from exc

    runtime_factory = cast(
        AgentRuntimeFactory,
        request.app.state.agent_runtime_factory,
    )
    adapter = request.app.state.mcp_adapter
    session_store = cast(SessionStore, request.app.state.session_store)
    for attempt in range(2):
        caller = RefreshingMCPCaller(
            adapter=adapter,
            refresh_service=refresh_service,
            db=db,
            credential=credential,
            session_store=session_store,
            agent_session_id=session.session_id,
        )
        action_proposal_tool: ActionProposalTool | None = None
        if action_conversation_id is not None:
            settings = cast(Settings, request.app.state.settings)
            action_proposal_tool = ActionProposalTool(
                service=ActionProposalService(
                    ActionGateway(
                        ActionRepository(),
                        ttl_seconds=settings.action_ttl_seconds,
                    ),
                    caller,
                ),
                session=db,
                session_id=session.session_id,
                site=session.site,
                requested_by=session.user_id,
                access_token=credential.access_token,
                conversation_id=action_conversation_id,
                telemetry=cast(Telemetry, request.app.state.telemetry),
            )
        try:
            return await runtime_factory.prepare(
                access_token=credential.access_token,
                expected_user=session.user_id,
                tool_caller=caller,
                action_proposal_tool=action_proposal_tool,
            )
        except AgentIdentityError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except MCPError as exc:
            if exc.code != "MCP_AUTH_FAILED" or attempt == 1:
                if exc.code == "MCP_AUTH_FAILED":
                    await _expire_agent_session(request, session)
                raise _mcp_http_exception(exc) from exc
            try:
                credential = await refresh_service.refresh_after_auth_failure(
                    db,
                    credential,
                )
            except TokenRefreshError as refresh_exc:
                await _expire_agent_session(request, session)
                raise _reauthentication_required(refresh_exc) from refresh_exc
    raise RuntimeError("Unreachable authentication retry state")


async def _expire_agent_session(request: Request, session: AgentSession) -> None:
    session_store = cast(SessionStore, request.app.state.session_store)
    await session_store.delete(session.session_id)


def _reauthentication_required(exc: TokenRefreshError) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "REAUTHENTICATION_REQUIRED", "message": str(exc)},
    )


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
            message="请说明要查询具体数据、做业绩/巡检分析，还是创建或修改 ERPNext 草稿。",
            conversation_id=conversation_id,
        )
    return None


def _conversation_messages(
    history: list[StoredMessage],
    current_message: str,
    *,
    summary: str = "",
) -> list[Msg]:
    messages: list[Msg] = []
    if summary:
        messages.append(AssistantMsg(name="conversation_memory", content=summary))
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
    *,
    action_proposal_tool: ActionProposalTool | None = None,
    telemetry: Telemetry | None = None,
    parent_context: Any | None = None,
    timeout_seconds: float | None = None,
    require_grounding: bool = False,
) -> AsyncIterator[str]:
    yield _sse("conversation", {"conversation_id": turn.conversation_id})

    async def persist_reply(content: str) -> None:
        await _persist_assistant(db, repository, turn.conversation_id, content)

    messages = turn.messages
    if action_proposal_tool is not None:
        messages = [
            AssistantMsg(
                name="runtime_action_context",
                content=(
                    "运行时规则：仓库简称不是缺失参数，不得要求用户补充完整后缀。"
                    "直接调用 erpnext_propose_draft_action，由服务端解析唯一完整 Warehouse。"
                ),
            ),
            *messages,
        ]
    async for event in _reply_events(
        agent,
        messages,
        on_complete=persist_reply,
        action_proposal_tool=action_proposal_tool,
        telemetry=telemetry,
        parent_context=parent_context,
        turn_id=turn.turn_id,
        timeout_seconds=timeout_seconds,
        require_grounding=require_grounding,
    ):
        yield event


async def _reply_events(
    agent: Agent,
    inputs: str | list[Msg],
    *,
    on_complete: Callable[[str], Awaitable[None]] | None = None,
    action_proposal_tool: ActionProposalTool | None = None,
    telemetry: Telemetry | None = None,
    parent_context: Any | None = None,
    turn_id: str | None = None,
    timeout_seconds: float | None = None,
    require_grounding: bool = False,
) -> AsyncIterator[str]:
    messages: Msg | list[Msg]
    if isinstance(inputs, str):
        messages = UserMsg(name="user", content=inputs)
    else:
        messages = inputs
    text_chunks: list[str] = []
    # Any tool activity in the stream grounds the turn; an ungrounded reply
    # that still contains numbers is a hallucination risk and gets flagged.
    saw_tool_activity = False
    telemetry = telemetry or Telemetry.disabled()
    attributes: dict[str, str] = {
        "agent_name": str(getattr(agent, "name", "agent")),
        "model_call_id": str(uuid.uuid4()),
    }
    if turn_id is not None:
        attributes["turn_id"] = turn_id
    with telemetry.span(
        "agent.model.reply_stream",
        attributes=attributes,
        parent_context=parent_context,
    ) as span:
        try:
            async for event in _bounded_reply_stream(agent, messages, timeout_seconds):
                if isinstance(event, TextBlockDeltaEvent):
                    delta = str(event.delta)
                    text_chunks.append(delta)
                    yield _sse("text_delta", {"delta": delta})
                elif isinstance(event, ToolCallStartEvent):
                    saw_tool_activity = True
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
                    elif (
                        require_grounding
                        and not saw_tool_activity
                        and _contains_numeric_claim(reply_text)
                    ):
                        # The model answered with numbers it never queried.
                        # Stream the warning so it lands in history too;
                        # the alternative — silent confident fabrication — is worse.
                        text_chunks.append(UNROUNDED_REPLY_WARNING)
                        reply_text = "".join(text_chunks).strip()
                        yield _sse("text_delta", {"delta": UNROUNDED_REPLY_WARNING})
                    action = (
                        action_proposal_tool.record
                        if action_proposal_tool is not None
                        else None
                    )
                    if action is not None:
                        appendix = action_summary_markdown(action)
                        text_chunks.append(appendix)
                        reply_text = "".join(text_chunks).strip()
                        yield _sse("text_delta", {"delta": appendix})
                    if on_complete is not None:
                        await on_complete(reply_text)
                    if action is not None:
                        yield _sse("action_required", action_public_payload(action))
                    reason = getattr(
                        event.finished_reason,
                        "value",
                        str(event.finished_reason),
                    )
                    set_span_result(span, "OK")
                    yield _sse("done", {"finished_reason": reason})
        except TimeoutError:
            set_span_result(span, "AGENT_TURN_TIMEOUT", error=True)
            if on_complete is not None:
                # Persist what the model managed to stream before the budget
                # expired so history is diagnosable; never mask the timeout.
                partial = "".join(text_chunks).strip()
                content = (
                    f"{partial}\n\n{TURN_TIMEOUT_FALLBACK}" if partial else TURN_TIMEOUT_FALLBACK
                )
                try:
                    await on_complete(content)
                except Exception:
                    logger.warning("Failed to persist the partial timed-out reply")
            yield _sse(
                "error",
                {"code": "AGENT_TURN_TIMEOUT", "message": "Agent turn timed out"},
            )
        except Exception:
            set_span_result(span, "AGENT_REPLY_FAILED", error=True)
            yield _sse(
                "error",
                {"code": "AGENT_REPLY_FAILED", "message": "Agent reply failed"},
            )


async def _bounded_reply_stream(
    agent: Agent,
    messages: Msg | list[Msg],
    timeout_seconds: float | None,
) -> AsyncIterator[Any]:
    if timeout_seconds is None:
        async for event in agent.reply_stream(messages):
            yield event
        return
    async with asyncio.timeout(timeout_seconds):
        async for event in agent.reply_stream(messages):
            yield event


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"
