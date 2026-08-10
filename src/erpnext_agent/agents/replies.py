from __future__ import annotations

from agentscope.message import Msg, TextBlock


def assistant_text(message: Msg) -> str:
    return "".join(
        block.text
        for block in message.content
        if isinstance(block, TextBlock)
    ).strip()
