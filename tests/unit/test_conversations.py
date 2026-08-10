from datetime import UTC, datetime

from erpnext_agent.conversations.repository import (
    StoredMessage,
    conversation_title,
    trim_context,
)


def message(sequence: int, content: str) -> StoredMessage:
    return StoredMessage(
        message_id=str(sequence),
        conversation_id="conversation",
        sequence=sequence,
        role="user" if sequence % 2 else "assistant",
        content=content,
        created_at=datetime.now(UTC),
    )


def test_trim_context_keeps_newest_contiguous_messages() -> None:
    messages = [message(1, "a" * 5), message(2, "b" * 5), message(3, "c" * 5)]

    selected = trim_context(messages, max_chars=10)

    assert [item.sequence for item in selected] == [2, 3]


def test_trim_context_does_not_send_partial_messages() -> None:
    messages = [message(1, "short"), message(2, "x" * 20)]

    assert trim_context(messages, max_chars=10) == []


def test_conversation_title_normalizes_and_truncates_first_user_message() -> None:
    assert conversation_title(None) == "新对话"
    assert conversation_title("  查询\n最近的  销售订单 ") == "查询 最近的 销售订单"
    assert conversation_title("123456", max_length=4) == "1234…"
