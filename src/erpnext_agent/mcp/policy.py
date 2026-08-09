from __future__ import annotations

READ_TOOLS = frozenset(
    {
        "erpnext_health",
        "erpnext_get_current_user",
        "erpnext_search_doctypes",
        "erpnext_get_doctype_schema",
        "erpnext_get_list",
        "erpnext_get_doc",
        "erpnext_get_count",
        "erpnext_get_stock_balance",
        "erpnext_get_customer_summary",
        "erpnext_get_supplier_summary",
        "erpnext_get_receivables_summary",
    }
)

WRITE_TOOLS = frozenset({"erpnext_create_draft", "erpnext_update_draft"})
EXPECTED_TOOLS = READ_TOOLS | WRITE_TOOLS

DATA_AGENT_TOOLS = READ_TOOLS - {"erpnext_health", "erpnext_get_current_user"}
PATROL_AGENT_TOOLS = DATA_AGENT_TOOLS
ACTION_AGENT_TOOLS = READ_TOOLS | WRITE_TOOLS
ORCHESTRATOR_TOOLS: frozenset[str] = frozenset()

FORBIDDEN_TOOLS = frozenset(
    {
        "erpnext_submit_doc",
        "erpnext_cancel_doc",
        "erpnext_delete_doc",
    }
)


class ToolPolicyError(PermissionError):
    pass


def tools_for_agent(agent_name: str) -> frozenset[str]:
    policies = {
        "data_agent": DATA_AGENT_TOOLS,
        "action_agent": ACTION_AGENT_TOOLS,
        "patrol_agent": PATROL_AGENT_TOOLS,
        "orchestrator": ORCHESTRATOR_TOOLS,
    }
    try:
        return policies[agent_name]
    except KeyError as exc:
        raise ToolPolicyError(f"Unknown agent policy: {agent_name}") from exc


def assert_tool_allowed(agent_name: str, tool_name: str) -> None:
    if tool_name not in tools_for_agent(agent_name):
        raise ToolPolicyError(f"{agent_name} cannot call {tool_name}")

