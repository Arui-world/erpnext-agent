from agentscope.message import TextBlock, UserMsg

from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.api.chat import _sse


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
