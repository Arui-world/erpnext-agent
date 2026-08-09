import pytest

from erpnext_agent.mcp.policy import WRITE_TOOLS, ToolPolicyError, assert_tool_allowed


def test_data_agent_has_no_write_tools() -> None:
    for tool in WRITE_TOOLS:
        with pytest.raises(ToolPolicyError):
            assert_tool_allowed("data_agent", tool)


def test_action_agent_can_use_draft_write_tools() -> None:
    for tool in WRITE_TOOLS:
        assert_tool_allowed("action_agent", tool)
