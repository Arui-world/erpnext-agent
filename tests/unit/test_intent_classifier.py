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

    async def generate_structured_output(
        self,
        messages: list[object],
        structured_model: object,
    ) -> FakeStructuredResponse:
        self.calls += 1
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
