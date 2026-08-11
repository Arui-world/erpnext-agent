import pytest

from erpnext_agent.mcp.policy import (
    READ_TOOLS,
    WRITE_TOOLS,
    ToolPolicyError,
    assert_tool_allowed,
)


def test_data_agent_has_no_write_tools() -> None:
    for tool in WRITE_TOOLS:
        with pytest.raises(ToolPolicyError):
            assert_tool_allowed("data_agent", tool)


def test_action_agent_can_use_draft_write_tools() -> None:
    for tool in WRITE_TOOLS:
        assert_tool_allowed("action_agent", tool)


def test_item_stock_by_warehouses_is_a_read_tool_for_business_agents() -> None:
    tool = "erpnext_get_item_stock_by_warehouses"
    assert tool in READ_TOOLS
    for agent_name in ("data_agent", "patrol_agent", "action_agent"):
        assert_tool_allowed(agent_name, tool)
    with pytest.raises(ToolPolicyError):
        assert_tool_allowed("orchestrator", tool)
