import pytest

from erpnext_agent.agents.orchestrator import Intent, IntentGate


@pytest.mark.parametrize("message", ["提交销售订单", "删除这个单据", "cancel PO-1"])
def test_forbidden_operations_are_denied(message: str) -> None:
    assert IntentGate().route(message).intent == Intent.DENY


def test_draft_creation_routes_to_action_agent() -> None:
    decision = IntentGate().route("创建一张物料需求草稿")
    assert decision.intent == Intent.ACTION
    assert decision.target_agent == "action_agent"


def test_read_routes_to_data_agent() -> None:
    decision = IntentGate().route("查询本月销售订单数量")
    assert decision.intent == Intent.DATA
    assert decision.target_agent == "data_agent"

