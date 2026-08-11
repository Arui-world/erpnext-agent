from erpnext_agent.agents.prompts import DATA_SYSTEM_PROMPT


def test_data_agent_uses_aggregate_tool_when_warehouse_is_missing() -> None:
    assert "第一个且唯一个工具调用必须是" in DATA_SYSTEM_PROMPT
    assert "erpnext_get_item_stock_by_warehouses" in DATA_SYSTEM_PROMPT
    assert '{"item_code":"ITEM-001"}' in DATA_SYSTEM_PROMPT
    assert "不得先行校验 Item" in DATA_SYSTEM_PROMPT
    assert "warehouses 和 totals" in DATA_SYSTEM_PROMPT
    assert "不得汇总库存价值" in DATA_SYSTEM_PROMPT


def test_data_agent_keeps_exact_warehouse_stock_tool_path() -> None:
    assert "erpnext_get_stock_balance 查询该仓库" in DATA_SYSTEM_PROMPT
