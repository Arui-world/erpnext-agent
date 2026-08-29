import asyncio
from dataclasses import dataclass

import pytest

from erpnext_agent.agents.intent_classifier import IntentClassifier
from erpnext_agent.agents.orchestrator import Intent


@dataclass
class FakeStructuredResponse:
    content: dict[str, str]


class FakeModel:
    def __init__(self, content: dict[str, str] | None = None, delay: float = 0) -> None:
        self.content = content or {"intent": "data", "reason": "业务信息查询"}
        self.delay = delay
        self.calls = 0
        self.messages: list[object] = []

    async def generate_structured_output(
        self,
        messages: list[object],
        structured_model: object,
    ) -> FakeStructuredResponse:
        self.calls += 1
        self.messages.extend(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeStructuredResponse(self.content)


@pytest.mark.asyncio
async def test_natural_language_is_routed_by_structured_model() -> None:
    model = FakeModel({"intent": "data", "reason": "用户想了解销售情况"})

    decision = await IntentClassifier(model).classify("帮我看看这个月销售情况")

    assert decision.intent == Intent.DATA
    assert decision.target_agent == "data_agent"
    assert model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_intent", "expected_agent"),
    [
        ({"intent": "action", "reason": "准备业务草稿"}, Intent.ACTION, "action_agent"),
        ({"intent": "patrol", "reason": "执行有界巡检"}, Intent.PATROL, "patrol_agent"),
        ({"intent": "clarify", "reason": "信息不足"}, Intent.CLARIFY, None),
    ],
)
async def test_structured_classifier_supports_each_non_deny_route(
    content: dict[str, str],
    expected_intent: Intent,
    expected_agent: str | None,
) -> None:
    decision = await IntentClassifier(FakeModel(content)).classify("自然语言请求")

    assert decision.intent == expected_intent
    assert decision.target_agent == expected_agent


@pytest.mark.asyncio
async def test_model_clarify_preserves_previous_explicit_route() -> None:
    model = FakeModel({"intent": "clarify", "reason": "上下文不足"})

    decision = await IntentClassifier(model).classify(
        "Stores - TQC",
        ["帮我看看当前库存"],
    )

    assert decision.intent == Intent.DATA
    assert decision.reason == "follow-up to previous data intent"


@pytest.mark.asyncio
async def test_model_exception_degrades_to_legacy_route() -> None:
    class BrokenModel(FakeModel):
        async def generate_structured_output(
            self,
            messages: list[object],
            structured_model: object,
        ):
            self.calls += 1
            raise RuntimeError("provider unavailable")

    decision = await IntentClassifier(BrokenModel()).classify("查看当前库存")

    assert decision.intent == Intent.DATA
    assert decision.target_agent == "data_agent"


@pytest.mark.asyncio
async def test_extra_structured_fields_are_rejected() -> None:
    model = FakeModel({"intent": "data", "reason": "ok", "route": "unsafe"})

    decision = await IntentClassifier(model).classify("查询库存")

    assert decision.intent == Intent.DATA
    assert decision.reason == "read-only intent detected"


@pytest.mark.asyncio
async def test_empty_message_does_not_call_model() -> None:
    model = FakeModel()

    decision = await IntentClassifier(model).classify("   ")

    assert decision.intent == Intent.CLARIFY
    assert model.calls == 0


@pytest.mark.asyncio
async def test_model_context_is_bounded_to_recent_messages() -> None:
    model = FakeModel()
    previous = [f"message-{index}-" + ("x" * 2000) for index in range(10)]

    await IntentClassifier(model).classify("查看库存", previous)

    rendered = str(model.messages[-1])
    assert "message-9" in rendered
    assert "message-3" not in rendered


@pytest.mark.asyncio
async def test_model_reason_is_returned_for_observability() -> None:
    model = FakeModel({"intent": "patrol", "reason": "按用户目标选择巡检"})

    decision = await IntentClassifier(model).classify("帮我检查风险")

    assert decision.reason == "按用户目标选择巡检"


@pytest.mark.asyncio
async def test_deny_is_deterministic_and_never_sent_to_model() -> None:
    model = FakeModel({"intent": "action", "reason": "模型误判"})

    decision = await IntentClassifier(model).classify("帮我提交这个订单")

    assert decision.intent == Intent.DENY
    assert model.calls == 0


@pytest.mark.asyncio
async def test_invalid_structured_result_degrades_to_legacy_gate() -> None:
    model = FakeModel({"intent": "not-a-route", "reason": "invalid"})

    decision = await IntentClassifier(model).classify("查看当前库存")

    assert decision.intent == Intent.DATA
    assert decision.reason == "read-only intent detected"


@pytest.mark.asyncio
async def test_classifier_timeout_degrades_without_blocking_request() -> None:
    model = FakeModel(delay=0.05)

    decision = await IntentClassifier(model, timeout_seconds=0.001).classify("帮我看看销售情况")

    assert decision.intent == Intent.CLARIFY
    assert decision.reason == "intent is incomplete"


def test_classifier_prompt_defines_data_patrol_boundary() -> None:
    from erpnext_agent.agents.intent_classifier import INTENT_CLASSIFIER_SYSTEM_PROMPT

    assert "跨期比较" in INTENT_CLASSIFIER_SYSTEM_PROMPT
    assert "业绩回顾" in INTENT_CLASSIFIER_SYSTEM_PROMPT
    assert "环比" in INTENT_CLASSIFIER_SYSTEM_PROMPT
    assert "给出下一步建议" in INTENT_CLASSIFIER_SYSTEM_PROMPT
    # Write-safety delegation to the deterministic layer stays intact.
    assert "提交、作废、删除" in INTENT_CLASSIFIER_SYSTEM_PROMPT
