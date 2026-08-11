from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from erpnext_agent.api.chat import ModelChatRequest, _conversation_messages
from erpnext_agent.conversations.repository import StoredMessage

WEB_ROOT = Path(__file__).parents[2] / "src" / "erpnext_agent" / "web"


def test_database_messages_preserve_multi_turn_roles() -> None:
    now = datetime.now(UTC)
    history = [
        StoredMessage("1", "conversation", 1, "user", "什么是 Agent？", now),
        StoredMessage(
            "2",
            "conversation",
            2,
            "assistant",
            "Agent 是能够执行任务的系统。",
            now,
        ),
    ]

    messages = _conversation_messages(history, "继续说明")

    assert [message.role for message in messages] == ["user", "assistant", "user"]
    assert messages[-1].get_text_content() == "继续说明"


def test_model_chat_rejects_oversized_message() -> None:
    with pytest.raises(ValidationError, match="String should have at most 8000 characters"):
        ModelChatRequest(message="x" * 8_001)


def test_chat_ui_contains_required_layout_and_local_assets() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
    javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    markdown = (WEB_ROOT / "markdown.js").read_text(encoding="utf-8")

    assert 'id="conversation"' in html
    assert 'id="composer"' in html
    assert 'href="/assets/styles.css"' in html
    assert 'src="/assets/markdown.js"' in html
    assert ".message-row.user" in css
    assert ".markdown-table-wrap" in css
    assert ".markdown-body table" in css
    assert "margin-left: auto" in css
    assert "/chat/model/stream" in javascript
    assert "/chat/stream" in javascript
    assert "/chat/history" in javascript
    assert "/chat/conversations" in javascript
    assert "conversation_id" in javascript
    assert "historyForRequest" not in javascript
    assert 'id="new-conversation-button"' in html
    assert 'id="conversation-list"' in html
    assert ".conversation-item.active" in css
    assert "SafeMarkdown.renderMarkdown" in javascript
    assert "bubble.innerHTML" not in javascript
    assert "documentRef.createTextNode" in markdown
    assert '"javascript:"' not in markdown
