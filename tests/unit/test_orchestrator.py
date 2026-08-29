import pytest

from erpnext_agent.agents.orchestrator import Intent, IntentGate
from erpnext_agent.api.chat import _fixed_policy_response


@pytest.mark.parametrize("message", ["提交销售订单", "删除这个单据", "cancel PO-1"])
def test_forbidden_operations_are_denied(message: str) -> None:
    assert IntentGate().route(message).intent == Intent.DENY


def test_draft_creation_routes_to_action_agent() -> None:
    decision = IntentGate().route("创建一张物料需求草稿")
    assert decision.intent == Intent.ACTION
    assert decision.target_agent == "action_agent"
    assert _fixed_policy_response(decision, "conversation-1") is None


def test_read_routes_to_data_agent() -> None:
    decision = IntentGate().route("查询本月销售订单数量")
    assert decision.intent == Intent.DATA
    assert decision.target_agent == "data_agent"


@pytest.mark.parametrize(
    "message",
    [
        "本月销售订单数量环比上个月怎么样",
        "分析一下最近的销售趋势",
        "库存补货建议",
        "看看本季度业绩",
    ],
)
def test_comparison_and_analysis_messages_route_to_patrol_agent(message: str) -> None:
    decision = IntentGate().route(message)
    assert decision.intent == Intent.PATROL
    assert decision.target_agent == "patrol_agent"


def test_single_fact_questions_do_not_route_to_patrol() -> None:
    # 库存/查询/数量 alone must keep plain fact lookups on the Data Agent.
    for message in ("查看 test item1 的库存", "查询本月销售订单数量"):
        assert IntentGate().route(message).intent == Intent.DATA


def test_draft_request_keeps_action_priority_over_suggestion_keyword() -> None:
    decision = IntentGate().route("创建一张物料需求草稿并给出建议")
    assert decision.intent == Intent.ACTION


def test_incomplete_follow_up_inherits_previous_data_intent() -> None:
    decision = IntentGate().route_with_context(
        "Stores - TQC",
        ["查看当前库存", "test item1在仓库的库存"],
    )
    assert decision.intent == Intent.DATA
    assert decision.target_agent == "data_agent"


def test_explicit_current_safety_intent_overrides_history() -> None:
    decision = IntentGate().route_with_context(
        "删除这个单据",
        ["查看当前库存"],
    )
    assert decision.intent == Intent.DENY
