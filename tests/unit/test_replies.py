from agentscope.event import ReplyEndEvent
from agentscope.message import TextBlock, UserMsg

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
