import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentscope.event import ReplyEndEvent, TextBlockDeltaEvent, ToolCallStartEvent
from agentscope.message import AssistantMsg, TextBlock, ToolResultBlock, UserMsg

from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.proposal import action_summary_markdown
from erpnext_agent.agents.orchestrator import Intent, RouteDecision
from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.api.chat import (
    EMPTY_REPLY_FALLBACK,
    TURN_TIMEOUT_FALLBACK,
    UNROUNDED_REPLY_WARNING,
    _context_has_tool_result,
    _fixed_policy_response,
    _reply_events,
    _sse,
)


def test_assistant_text_joins_text_blocks() -> None:
    message = UserMsg(
        name="assistant",
        content=[TextBlock(text="模型"), TextBlock(text="回复")],
    )
    assert assistant_text(message) == "模型回复"


def test_sse_serialization_does_not_escape_chinese() -> None:
    assert _sse("text_delta", {"delta": "成功"}) == (
        'event: text_delta\ndata: {"delta":"成功"}\n\n'
    )


async def test_empty_stream_reply_emits_and_persists_fallback() -> None:
    class EmptyReplyAgent:
        async def reply_stream(self, _message: UserMsg):
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    persisted: list[str] = []

    async def persist(content: str) -> None:
        persisted.append(content)

    events = [
        event
        async for event in _reply_events(  # type: ignore[arg-type]
            EmptyReplyAgent(),
            "查看当前库存",
            on_complete=persist,
        )
    ]

    assert events == [
        _sse("text_delta", {"delta": EMPTY_REPLY_FALLBACK}),
        _sse("done", {"finished_reason": "completed"}),
    ]
    assert persisted == [EMPTY_REPLY_FALLBACK]


async def test_action_stream_emits_persistent_summary_before_action_event() -> None:
    class ActionReplyAgent:
        async def reply_stream(self, _message: UserMsg):
            yield TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="请确认预览。")
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    now = datetime.now(UTC)
    record = ActionRecord(
        action_id="action-1",
        session_id="session-1",
        site="dev.localhost",
        requested_by="user@example.com",
        tool_name="erpnext_create_draft",
        canonical_arguments={"doctype": "Material Request", "payload": {}},
        arguments_sha256="a" * 64,
        preview={"title": "创建 Material Request 草稿"},
        source_versions={},
        idempotency_key="fixed-idempotency-key",
        status=ActionStatus.PENDING.value,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    persisted: list[str] = []

    async def persist(content: str) -> None:
        persisted.append(content)

    events = [
        event
        async for event in _reply_events(  # type: ignore[arg-type]
            ActionReplyAgent(),
            "创建物料需求草稿",
            on_complete=persist,
            action_proposal_tool=SimpleNamespace(record=record),  # type: ignore[arg-type]
        )
    ]

    assert events[0] == _sse("text_delta", {"delta": "请确认预览。"})
    assert events[1] == _sse("text_delta", {"delta": action_summary_markdown(record)})
    assert events[2].startswith("event: action_required\n")
    action_payload = json.loads(events[2].split("data: ", 1)[1])
    assert action_payload["action_id"] == record.action_id
    assert events[3] == _sse("done", {"finished_reason": "completed"})
    assert persisted == [("请确认预览。" + action_summary_markdown(record)).strip()]


async def test_stream_timeout_returns_stable_error_event() -> None:
    class SlowReplyAgent:
        async def reply_stream(self, _message: UserMsg):
            await asyncio.sleep(0.05)
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    events = [
        event
        async for event in _reply_events(  # type: ignore[arg-type]
            SlowReplyAgent(),
            "查看当前库存",
            timeout_seconds=0.001,
        )
    ]

    assert events == [
        _sse(
            "error",
            {"code": "AGENT_TURN_TIMEOUT", "message": "Agent turn timed out"},
        )
    ]


async def test_stream_timeout_persists_partial_text_with_diagnostic_marker() -> None:
    class StallingAgent:
        async def reply_stream(self, _message: UserMsg):
            yield TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="已查到部分数据")
            await asyncio.sleep(0.05)
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    persisted: list[str] = []

    async def persist(content: str) -> None:
        persisted.append(content)

    events = [
        event
        async for event in _reply_events(  # type: ignore[arg-type]
            StallingAgent(),
            "分析本月业绩",
            on_complete=persist,
            timeout_seconds=0.001,
        )
    ]

    assert persisted == [f"已查到部分数据\n\n{TURN_TIMEOUT_FALLBACK}"]
    assert events[-1] == _sse(
        "error",
        {"code": "AGENT_TURN_TIMEOUT", "message": "Agent turn timed out"},
    )


async def test_stream_timeout_persists_marker_only_without_partial_text() -> None:
    class StallingAgent:
        async def reply_stream(self, _message: UserMsg):
            await asyncio.sleep(0.05)
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    persisted: list[str] = []

    async def persist(content: str) -> None:
        persisted.append(content)

    _ = [
        event
        async for event in _reply_events(  # type: ignore[arg-type]
            StallingAgent(),
            "分析本月业绩",
            on_complete=persist,
            timeout_seconds=0.001,
        )
    ]

    assert persisted == [TURN_TIMEOUT_FALLBACK]


async def test_stream_timeout_persistence_failure_does_not_mask_timeout() -> None:
    class StallingAgent:
        async def reply_stream(self, _message: UserMsg):
            await asyncio.sleep(0.05)
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    async def broken_persist(_content: str) -> None:
        raise RuntimeError("database is down")

    events = [
        event
        async for event in _reply_events(  # type: ignore[arg-type]
            StallingAgent(),
            "分析本月业绩",
            on_complete=broken_persist,
            timeout_seconds=0.001,
        )
    ]

    assert events == [
        _sse(
            "error",
            {"code": "AGENT_TURN_TIMEOUT", "message": "Agent turn timed out"},
        )
    ]


def test_clarify_fixed_response_mentions_analysis_routes() -> None:
    decision = RouteDecision(intent=Intent.CLARIFY, target_agent=None, reason="incomplete")
    response = _fixed_policy_response(decision, "conversation-1")
    assert response is not None
    assert "业绩/巡检分析" in response.message


async def _drain(agent: object, **kwargs: object) -> tuple[list[str], list[str]]:
    persisted: list[str] = []

    async def persist(content: str) -> None:
        persisted.append(content)

    events = [
        event
        async for event in _reply_events(  # type: ignore[arg-type]
            agent,
            "本月销售订单数量环比上个月怎么样",
            on_complete=persist,
            **kwargs,  # type: ignore[arg-type]
        )
    ]
    return events, persisted


def _text_agent(events_before_text: list[object] | None = None) -> object:
    class Agent:
        name = "data_agent"

        async def reply_stream(self, _message: object):
            for extra in events_before_text or []:
                yield extra
            yield TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="共 409 张。")
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    return Agent()


def _clarify_agent() -> object:
    class Agent:
        name = "patrol_agent"

        async def reply_stream(self, _message: object):
            yield TextBlockDeltaEvent(
                reply_id="reply-1", block_id="text-1", delta="请问低库存的阈值是多少？"
            )
            yield ReplyEndEvent(session_id="session-1", reply_id="reply-1")

    return Agent()


async def test_stream_numeric_answer_without_any_tool_call_is_flagged() -> None:
    events, persisted = await _drain(_text_agent(), require_grounding=True)
    assert any("未经查询验证" in event for event in events)
    assert persisted and UNROUNDED_REPLY_WARNING in persisted[-1]
    assert "共 409 张。" in persisted[-1]  # original text kept, warning appended


async def test_stream_numeric_answer_with_tool_call_is_not_flagged() -> None:
    call = ToolCallStartEvent(
        reply_id="reply-1",
        tool_call_id="call-1",
        tool_call_name="erpnext_get_count",
    )
    events, persisted = await _drain(_text_agent([call]), require_grounding=True)
    assert not any("未经查询验证" in event for event in events)
    assert persisted == ["共 409 张。"]


async def test_stream_numeric_answer_without_grounding_requirement_is_untouched() -> None:
    events, persisted = await _drain(_text_agent())
    assert not any("未经查询验证" in event for event in events)
    assert persisted == ["共 409 张。"]


async def test_stream_digit_free_clarification_is_not_flagged() -> None:
    events, persisted = await _drain(_clarify_agent(), require_grounding=True)
    assert not any("未经查询验证" in event for event in events)
    assert persisted == ["请问低库存的阈值是多少？"]


def test_context_has_tool_result_scans_agent_state() -> None:
    grounded = SimpleNamespace(
        state=SimpleNamespace(
            context=[
                AssistantMsg(
                    name="data_agent",
                    content=[
                        ToolResultBlock(
                            id="call-1",
                            name="erpnext_get_count",
                            output=[TextBlock(text='{"ok":true}')],
                        )
                    ],
                )
            ]
        )
    )
    ungrounded = SimpleNamespace(state=SimpleNamespace(context=[]))
    assert _context_has_tool_result(grounded) is True  # type: ignore[arg-type]
    assert _context_has_tool_result(ungrounded) is False  # type: ignore[arg-type]
