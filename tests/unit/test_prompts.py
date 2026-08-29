from erpnext_agent.agents.prompts import (
    ACTION_SYSTEM_PROMPT,
    DATA_SYSTEM_PROMPT,
    PATROL_SYSTEM_PROMPT,
)


def test_data_agent_uses_item_group_aggregate_tool() -> None:
    assert "erpnext_get_item_group_low_stock" in DATA_SYSTEM_PROMPT
    assert "只调用一次" in DATA_SYSTEM_PROMPT
    assert "自动包含子物料组" in DATA_SYSTEM_PROMPT
    assert "回答时必须引用返回的 scope" in DATA_SYSTEM_PROMPT
    # The client-side N+1 workflow must be gone.
    assert "对返回的每个真实 item_code 分别调用" not in DATA_SYSTEM_PROMPT


def test_data_agent_gets_one_corrective_retry_on_empty_lists() -> None:
    assert "最多允许做一次核实性查询" in DATA_SYSTEM_PROMPT
    assert "like 模糊匹配" in DATA_SYSTEM_PROMPT
    # The empty-result honesty and anti-fabrication rules stay.
    assert "空结果就是结果" in DATA_SYSTEM_PROMPT
    assert "绝不编造物料编码或库存数量" in DATA_SYSTEM_PROMPT


def test_patrol_agent_knows_item_group_aggregate_tool() -> None:
    assert "erpnext_get_item_group_low_stock" in PATROL_SYSTEM_PROMPT


def test_data_agent_uses_aggregate_tool_when_warehouse_is_missing() -> None:
    assert "第一个且唯一个工具调用必须是" in DATA_SYSTEM_PROMPT
    assert "erpnext_get_item_stock_by_warehouses" in DATA_SYSTEM_PROMPT
    assert '{"item_code":"ITEM-001"}' in DATA_SYSTEM_PROMPT
    assert "不得先行校验 Item" in DATA_SYSTEM_PROMPT
    assert "warehouses 和 totals" in DATA_SYSTEM_PROMPT
    assert "不得汇总库存价值" in DATA_SYSTEM_PROMPT


def test_data_agent_keeps_exact_warehouse_stock_tool_path() -> None:
    assert "erpnext_get_stock_balance 查询该仓库" in DATA_SYSTEM_PROMPT
    assert "绝不编造物料编码或库存数量" in DATA_SYSTEM_PROMPT


def test_action_agent_can_only_create_persistent_proposals() -> None:
    assert "erpnext_propose_draft_action" in ACTION_SYSTEM_PROMPT
    assert "不得包含\nidempotency_key" in ACTION_SYSTEM_PROMPT
    assert "不会写入 ERPNext" in ACTION_SYSTEM_PROMPT
    assert "不得直接调用 ERPNext 写工具" in ACTION_SYSTEM_PROMPT
    assert "不能声称已提交、已过账或已预占库存" in ACTION_SYSTEM_PROMPT
    assert "不要因此追问" in ACTION_SYSTEM_PROMPT
    assert "手工补充“仓库 - rw”等完整名称" in ACTION_SYSTEM_PROMPT
    assert "不属于缺失参数" in ACTION_SYSTEM_PROMPT
