import pytest

from erpnext_agent.mcp.policy import (
    ACTION_AGENT_TOOLS,
    ANALYTICS_TOOL_NAME,
    DATA_AGENT_TOOLS,
    EXPECTED_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    ToolPolicyError,
    assert_tool_allowed,
    tools_for_agent,
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


def test_item_group_low_stock_is_a_read_tool_for_business_agents() -> None:
    tool = "erpnext_get_item_group_low_stock"
    assert tool in READ_TOOLS
    for agent_name in ("data_agent", "patrol_agent", "action_agent"):
        assert_tool_allowed(agent_name, tool)
    with pytest.raises(ToolPolicyError):
        assert_tool_allowed("orchestrator", tool)


def test_analytics_tool_is_a_local_read_tool_outside_the_mcp_contract() -> None:
    # The MCP adapter enforces strict set equality against tools/list; a local
    # tool must never appear in the MCP-facing sets.
    assert ANALYTICS_TOOL_NAME == "erpnext_analytics"
    assert ANALYTICS_TOOL_NAME not in READ_TOOLS
    assert ANALYTICS_TOOL_NAME not in EXPECTED_TOOLS
    for agent_name in ("data_agent", "patrol_agent"):
        assert_tool_allowed(agent_name, ANALYTICS_TOOL_NAME)
    for agent_name in ("action_agent", "orchestrator"):
        with pytest.raises(ToolPolicyError):
            assert_tool_allowed(agent_name, ANALYTICS_TOOL_NAME)


def test_tools_for_agent_merges_only_permitted_local_tools() -> None:
    assert tools_for_agent("data_agent") == DATA_AGENT_TOOLS | {ANALYTICS_TOOL_NAME}
    assert tools_for_agent("patrol_agent") == DATA_AGENT_TOOLS | {ANALYTICS_TOOL_NAME}
    assert tools_for_agent("action_agent") == ACTION_AGENT_TOOLS
    assert tools_for_agent("orchestrator") == frozenset()
