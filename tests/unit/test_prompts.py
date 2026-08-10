from erpnext_agent.agents.prompts import DATA_SYSTEM_PROMPT


def test_data_agent_queries_positive_bins_when_warehouse_is_missing() -> None:
    assert "但未指定 warehouse 时，不得询问仓库名称" in DATA_SYSTEM_PROMPT
    assert "erpnext_get_list 查询 Bin" in DATA_SYSTEM_PROMPT
    assert "actual_qty 大于 0" in DATA_SYSTEM_PROMPT
    assert "返回所有正库存仓库" in DATA_SYSTEM_PROMPT


def test_data_agent_keeps_exact_warehouse_stock_tool_path() -> None:
    assert "erpnext_get_stock_balance 查询该仓库" in DATA_SYSTEM_PROMPT
