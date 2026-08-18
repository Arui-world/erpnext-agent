import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentscope.event import ReplyEndEvent, TextBlockDeltaEvent
from agentscope.message import TextBlock, UserMsg

from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.proposal import action_summary_markdown
from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.api.chat import EMPTY_REPLY_FALLBACK, _reply_events, _sse


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
